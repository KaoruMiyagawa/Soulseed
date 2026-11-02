"""本机只读查看 Migration 12 隔离的会话，并保存不含正文的确认清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ANALYSIS_DATABASE = Path(r"D:\Soulseed-Backups\G1-20261102\analysis\soulseed-analysis.db")
EXPECTED_ANALYSIS_SHA256 = "C87481DC0D07719CD7A2D91B0E370EA674560615ED20F75EC80AEDF425C4F630"
MEMORY_ID = "9445ed85-11e3-436d-b305-b1a5f38715d2"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
METHOD = "local_terminal"
MAX_PREVIEW_CHARS = 320
MESSAGE_PAGE_CHARS = 600


class SnapshotError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def open_readonly_database(path: Path, expected_sha256: str) -> sqlite3.Connection:
    """Verify bytes first, then open immutable/read-only and validate integrity."""
    if path.is_symlink() or not path.is_file():
        raise SnapshotError("分析数据库不存在或不是普通文件")
    resolved = path.resolve(strict=True)
    if sha256_file(resolved) != expected_sha256.upper():
        raise SnapshotError("分析数据库 SHA-256 与已核验基准不一致")

    uri = resolved.as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SnapshotError("SQLite quick_check 未通过")
        if any(connection.execute("PRAGMA foreign_key_check")):
            raise SnapshotError("SQLite foreign_key_check 发现违规")
        if sha256_file(resolved) != expected_sha256.upper():
            raise SnapshotError("数据库在只读验证期间发生变化")
        return connection
    except Exception:
        try:
            connection.close()
        except (UnboundLocalError, AttributeError):
            pass
        raise


def open_verified_analysis() -> sqlite3.Connection:
    """The CLI is intentionally pinned to the one reviewed analysis copy."""
    try:
        resolved = ANALYSIS_DATABASE.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError("已核验的分析副本不存在") from exc
    if resolved != ANALYSIS_DATABASE:
        raise SnapshotError("分析副本路径解析结果与固定来源不一致")
    connection = open_readonly_database(ANALYSIS_DATABASE, EXPECTED_ANALYSIS_SHA256)
    try:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        session_count = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE character_id IS NULL"
        ).fetchone()[0]
        message_count = connection.execute(
            "SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.character_id IS NULL"
        ).fetchone()[0]
        memory_exists = connection.execute(
            "SELECT 1 FROM memory_items WHERE id=? AND character_id IS NULL", (MEMORY_ID,)
        ).fetchone()
        source = connection.execute(
            """SELECT s.message_id,s.source_role,s.evidence_type,m.conversation_id,c.character_id
               FROM memory_sources s JOIN messages m ON m.id=s.message_id
               JOIN conversations c ON c.id=m.conversation_id WHERE s.memory_id=?""", (MEMORY_ID,)
        ).fetchall()
        if (version != 14 or session_count != 10 or message_count != 30 or memory_exists is None
                or len(source) != 1 or source[0]["source_role"] != "user"
                or source[0]["evidence_type"] != "explicit_fact" or source[0]["character_id"] is not None):
            raise SnapshotError("分析副本与已审计的 Migration 14 数据摘要不符")
        return connection
    except Exception:
        connection.close()
        raise


def snapshot_summary(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """SELECT c.id, COUNT(m.id) AS total,
             SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) AS user_count,
             SUM(CASE WHEN m.role='assistant' THEN 1 ELSE 0 END) AS assistant_count
           FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id
           WHERE c.character_id IS NULL GROUP BY c.id"""
    ).fetchall()
    return {
        "migration": int(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]),
        "unconfirmed_conversations": len(rows),
        "messages": sum(int(row["total"] or 0) for row in rows),
        "unconfirmed_memories": int(connection.execute(
            "SELECT COUNT(*) FROM memory_items WHERE character_id IS NULL"
        ).fetchone()[0]),
    }


def session_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT c.id,c.created_at,c.character_id,COUNT(m.id) AS message_count,
             SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) AS user_count,
             SUM(CASE WHEN m.role='assistant' THEN 1 ELSE 0 END) AS assistant_count
           FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id
           WHERE c.character_id IS NULL GROUP BY c.id ORDER BY c.created_at,c.id"""
    ).fetchall()


def _safe_terminal_text(value: str) -> str:
    # Prevent stored control/escape characters from affecting the local terminal.
    safe = []
    for char in value:
        code = ord(char)
        if char in "\n\r\t":
            safe.append(char)
        elif code < 32 or code == 127:
            safe.append("�")
        else:
            safe.append(char)
    return "".join(safe)


def _short_text(value: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    text = _safe_terminal_text(value.strip())
    return text if len(text) <= limit else text[:limit] + "…（截断）"


def preview_messages(connection: sqlite3.Connection, conversation_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT id,role,created_at,content FROM (
             SELECT id,role,created_at,content FROM messages WHERE conversation_id=?
             ORDER BY created_at,id LIMIT 2
           )
           UNION
           SELECT id,role,created_at,content FROM (
             SELECT id,role,created_at,content FROM messages WHERE conversation_id=?
             ORDER BY created_at DESC,id DESC LIMIT 2
           ) ORDER BY created_at,id""",
        (conversation_id, conversation_id),
    ).fetchall()


def save_message_decision(manifest: dict[str, Any], conversation_id: str, decision: str) -> None:
    if decision not in {"big_fish", "quarantined", "uncertain", "skipped"}:
        raise ValueError("invalid conversation decision")
    manifest["conversations"] = [
        row for row in manifest["conversations"] if row["conversation_id"] != conversation_id
    ]
    manifest["conversations"].append({
        "conversation_id": conversation_id,
        "decision": decision,
        "confirmed_at": datetime.now(UTC).isoformat(),
        "method": METHOD,
    })


def save_memory_decision(manifest: dict[str, Any], decision: str) -> None:
    if decision not in {"big_fish", "quarantined", "uncertain", "skipped"}:
        raise ValueError("invalid memory decision")
    manifest["memory"] = {
        "memory_id": MEMORY_ID,
        "decision": decision,
        "confirmed_at": datetime.now(UTC).isoformat(),
        "method": METHOD,
    }


def manifest_for_hash(analysis_sha256: str) -> dict[str, Any]:
    # Deliberately no field accepts or stores chat/memory text.
    return {"analysis_sha256": analysis_sha256.upper(), "conversations": [], "memory": None}


def _secure_directory(directory: Path, *, newly_created: bool) -> None:
    if os.name == "nt":
        if not newly_created:
            # Resume only a tool-owned directory containing this tool's list.
            allowed = {"ownership-confirmations.json"}
            if {p.name for p in directory.iterdir()} - allowed:
                raise RuntimeError("恢复目录包含非确认清单文件，已拒绝继续")
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, check=True).stdout
        match = re.search(r"S-1-\d+(?:-\d+)+", identity)
        if not match:
            raise RuntimeError("无法确认当前 Windows 用户 SID，未创建确认清单")
        if not newly_created:
            subprocess.run(["icacls", str(directory), "/reset", "/q"], capture_output=True, check=True)
        subprocess.run([
            "icacls", str(directory), "/inheritance:r", "/grant:r",
            f"*{match.group(0)}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "/q",
        ], capture_output=True, check=True)
    else:
        directory.chmod(0o700)


def _secure_file(path: Path) -> None:
    if os.name == "nt":
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, check=True).stdout
        match = re.search(r"S-1-\d+(?:-\d+)+", identity)
        if not match:
            raise RuntimeError("无法确认当前 Windows 用户 SID，未保存确认清单")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"*{match.group(0)}:F", "*S-1-5-18:F", "/q"],
                       capture_output=True, check=True)
    else:
        path.chmod(0o600)


def default_output_directory() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    parent = (local_app_data / "Soulseed" / "G3-B").resolve(strict=False)
    if parent == PROJECT_ROOT or PROJECT_ROOT in parent.parents:
        raise RuntimeError("本机确认清单目录解析到了 Git 仓库，已拒绝写入")
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = parent / f"confirmation-{stamp}"
    target.mkdir()
    try:
        _secure_directory(target, newly_created=True)
    except Exception:
        target.rmdir()
        raise
    return target


def resume_output_directory(path: Path) -> Path:
    directory = path.expanduser().resolve(strict=True)
    app_root = (Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
                / "Soulseed" / "G3-B").resolve(strict=False)
    try:
        directory.relative_to(app_root)
    except ValueError as exc:
        raise RuntimeError("恢复清单必须位于本机 Soulseed/G3-B 私有目录内") from exc
    if directory == PROJECT_ROOT or PROJECT_ROOT in directory.parents:
        raise RuntimeError("确认清单不得写入 Git 仓库")
    _secure_directory(directory, newly_created=False)
    return directory


def save_manifest(path: Path, manifest: dict[str, Any], expected_sha256: str) -> None:
    if sha256_file(ANALYSIS_DATABASE) != expected_sha256.upper():
        raise SnapshotError("分析副本哈希发生变化；未保存本次确认")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_suffix(".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        _secure_file(path)
    finally:
        if temp.exists():
            temp.unlink()


def load_manifest(path: Path, expected_sha256: str, valid_ids: set[str]) -> dict[str, Any]:
    if not path.is_file():
        return manifest_for_hash(expected_sha256)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if set(data) != {"analysis_sha256", "conversations", "memory"}:
            raise ValueError
        if data["analysis_sha256"] != expected_sha256.upper():
            raise ValueError
        if not isinstance(data["conversations"], list):
            raise ValueError
        for item in data["conversations"]:
            if (set(item) != {"conversation_id", "decision", "confirmed_at", "method"}
                    or item["conversation_id"] not in valid_ids
                    or item["decision"] not in {"big_fish", "quarantined", "uncertain", "skipped"}
                    or item["method"] != METHOD):
                raise ValueError
        memory = data["memory"]
        if memory is not None and (set(memory) != {"memory_id", "decision", "confirmed_at", "method"}
                or memory["memory_id"] != MEMORY_ID
                or memory["decision"] not in {"big_fish", "quarantined", "uncertain", "skipped"}
                or memory["method"] != METHOD):
            raise ValueError
        return data
    except Exception as exc:
        raise RuntimeError("现有确认清单格式、快照哈希或字段不符，已拒绝覆盖") from exc


def _show_preview(connection: sqlite3.Connection, conversation_id: str) -> None:
    rows = preview_messages(connection, conversation_id)
    if not rows:
        print("该会话没有消息。")
        return
    print("\n少量上下文（每条最多 320 字；这不是完整历史）：")
    for row in rows:
        print(f"[{row['created_at']} · {row['role']}] {_short_text(row['content'])}")


def _browse_messages(connection: sqlite3.Connection, conversation_id: str) -> None:
    count = connection.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)
    ).fetchone()[0]
    for offset in range(count):
        if input("按 Enter 显示下一条消息，输入 q 结束浏览：").strip().lower() == "q":
            return
        row = connection.execute(
            "SELECT created_at,role,content FROM messages WHERE conversation_id=? ORDER BY created_at,id LIMIT 1 OFFSET ?",
            (conversation_id, offset),
        ).fetchone()
        if row is None:
            return
        print(f"[{offset+1}/{count} · {row['created_at']} · {row['role']}]")
        text = _safe_terminal_text(row["content"])
        for start in range(0, max(1, len(text)), MESSAGE_PAGE_CHARS):
            print(text[start:start + MESSAGE_PAGE_CHARS])
            if start + MESSAGE_PAGE_CHARS < len(text):
                if input("按 Enter 查看本条后续片段，输入 q 停止：").strip().lower() == "q":
                    return
        if offset + 1 < count and input("按 Enter 查看下一条，输入 q 结束浏览：").strip().lower() == "q":
            return


def _choose_conversations(connection: sqlite3.Connection, manifest: dict[str, Any], out_file: Path) -> None:
    for row in session_rows(connection):
        existing = next((item for item in manifest["conversations"] if item["conversation_id"] == row["id"]), None)
        previous = existing["decision"] if existing else "未确认"
        print("\n" + "=" * 72)
        print(f"会话 ID：{row['id']}")
        print(f"创建时间：{row['created_at']} UTC；消息数：{row['message_count']}（用户 {row['user_count'] or 0} / 助手 {row['assistant_count'] or 0}）")
        print(f"当前数据库归属：未确认；本地清单已有选择：{previous}")
        if row["message_count"] == 0:
            print("这是空会话；时间不能作为归属依据，若确认大肥鱼需要额外键入 BIG_FISH。")
        print("若识别为多个数字角色混用，请选择保持隔离或暂时无法确认。")
        while True:
            choice = input("[p] 少量片段 [v] 逐条浏览 [b] 确认 big_fish [q] 保持隔离 [u] 暂时无法确认 [s] 跳过 [k] 保留现有选择：").strip().lower()
            if choice == "p":
                _show_preview(connection, row["id"])
            elif choice == "v":
                _browse_messages(connection, row["id"])
            elif choice == "b":
                if row["message_count"] == 0 and input("空会话再次确认：").strip() != "BIG_FISH":
                    print("未确认。")
                    continue
                decision = "big_fish"
                break
            elif choice in {"q", "u", "s"}:
                decision = {"q": "quarantined", "u": "uncertain", "s": "skipped"}[choice]
                break
            elif choice == "k" and existing:
                decision = existing["decision"]
                break
        save_message_decision(manifest, row["id"], decision)
        save_manifest(out_file, manifest, EXPECTED_ANALYSIS_SHA256)


def _show_memory(connection: sqlite3.Connection, source_session_decision: str | None) -> bool:
    memory = connection.execute(
        "SELECT id,memory_type,status,created_at,content FROM memory_items WHERE id=? AND character_id IS NULL",
        (MEMORY_ID,),
    ).fetchone()
    source = connection.execute(
        """SELECT s.message_id,s.source_role,s.evidence_type,s.created_at,m.conversation_id,m.content
           FROM memory_sources s JOIN messages m ON m.id=s.message_id
           WHERE s.memory_id=? ORDER BY s.created_at,s.message_id""", (MEMORY_ID,)
    ).fetchall()
    print("\n记忆单独确认")
    print(f"记忆 ID：{memory['id']}；类型：{memory['memory_type']}；状态：{memory['status']}；创建时间：{memory['created_at']}")
    print(f"来源记录数：{len(source)}；来源会话选择：{source_session_decision or '未确认'}")
    for item in source:
        print(f"来源消息 ID：{item['message_id']}；角色：{item['source_role']}；证据类型：{item['evidence_type']}；时间：{item['created_at']}")
        print(f"来源会话 ID：{item['conversation_id']}")
    print("记忆正文与来源片段默认隐藏；输入 v 后仅在本机终端查看。")
    viewed = input("查看记忆及来源片段？[y/N]：").strip().lower() == "y"
    if viewed:
        print("\n记忆内容：\n" + _safe_terminal_text(memory["content"]))
        for item in source:
            print("\n来源片段：\n" + _short_text(item["content"], 600))
    return viewed


def memory_can_be_assigned_to_big_fish(source_session_decision: str | None) -> bool:
    return source_session_decision == "big_fish"


def _choose_memory(connection: sqlite3.Connection, manifest: dict[str, Any], out_file: Path) -> None:
    memory_row = connection.execute(
        "SELECT id FROM memory_items WHERE id=? AND character_id IS NULL", (MEMORY_ID,)
    ).fetchone()
    if not memory_row:
        raise SnapshotError("待确认的记忆不在已核验副本中")
    sources = connection.execute(
        """SELECT m.conversation_id FROM memory_sources s JOIN messages m ON m.id=s.message_id
           WHERE s.memory_id=? ORDER BY s.created_at,s.message_id""", (MEMORY_ID,)
    ).fetchall()
    if len(sources) != 1:
        raise SnapshotError("记忆来源数与已审计摘要不符")
    source_id = sources[0]["conversation_id"]
    session_choice = next((x["decision"] for x in manifest["conversations"] if x["conversation_id"] == source_id), None)
    viewed_content = _show_memory(connection, session_choice)
    existing = manifest["memory"]
    previous = existing["decision"] if existing else "未确认"
    while True:
        prompt = f"记忆当前独立选择：{previous}。[b] 确认 big_fish [q] 保持隔离 [u] 暂时无法确认 [s] 跳过 [k] 保留现有选择："
        choice = input(prompt).strip().lower()
        if choice == "b":
            if not memory_can_be_assigned_to_big_fish(session_choice):
                print("来源会话尚未明确选择 big_fish；不能将记忆列为拟恢复到 big_fish。")
                continue
            if not viewed_content:
                print("请先查看记忆内容及来源片段，再单独确认该记忆。")
                continue
            if input("记忆再次确认（输入 CONFIRM MEMORY）：").strip() != "CONFIRM MEMORY":
                print("未确认。")
                continue
            decision = "big_fish"
            break
        elif choice in {"q", "u", "s"}:
            decision = {"q": "quarantined", "u": "uncertain", "s": "skipped"}[choice]
            break
        elif choice == "k" and existing and (existing["decision"] != "big_fish" or memory_can_be_assigned_to_big_fish(session_choice)):
            decision = existing["decision"]
            break
    save_memory_decision(manifest, decision)
    save_manifest(out_file, manifest, EXPECTED_ANALYSIS_SHA256)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="只验证固定分析副本并输出聚合统计")
    parser.add_argument("--resume", type=Path, help="继续写入本机 Soulseed/G3-B 下已有的私有清单目录")
    args = parser.parse_args()

    connection = None
    try:
        connection = open_verified_analysis()
        summary = snapshot_summary(connection)
        if args.verify_only:
            print(json.dumps({"analysis_sha256": EXPECTED_ANALYSIS_SHA256, **summary}, ensure_ascii=False))
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("确认工具只允许在本机交互式终端运行，禁止重定向正文输出")

        output_dir = resume_output_directory(args.resume) if args.resume else default_output_directory()
        out_file = output_dir / "ownership-confirmations.json"
        if out_file.is_symlink():
            raise RuntimeError("确认清单不能是符号链接")
        ids = {row["id"] for row in session_rows(connection)}
        if args.resume and not out_file.is_file():
            raise RuntimeError("指定恢复目录中没有确认清单")
        manifest = load_manifest(out_file, EXPECTED_ANALYSIS_SHA256, ids) if args.resume else manifest_for_hash(EXPECTED_ANALYSIS_SHA256)

        print("历史归属只读确认。数据库和正文不会写入确认文件，也不会发送到模型或网络服务。")
        print(f"快照 SHA-256：{EXPECTED_ANALYSIS_SHA256}；会话 {summary['unconfirmed_conversations']}；消息 {summary['messages']}。")
        print(f"私有确认目录：{output_dir}")
        _choose_conversations(connection, manifest, out_file)
        _choose_memory(connection, manifest, out_file)
        _print_recovery_scope(connection, manifest)
        print("确认清单已保存。它只记录选择、时间、方式和快照哈希，不是数据库恢复授权。")
        print(f"清单路径：{out_file}")
        return 0
    except KeyboardInterrupt:
        print("\n已停止；之前保存的确认选择仍可用 --resume 继续。")
        return 130
    except Exception as exc:
        # Error output intentionally omits exception text and database content.
        print(f"已安全停止：{type(exc).__name__}。未输出正文或调用外部服务。", file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()


def _print_recovery_scope(connection: sqlite3.Connection, manifest: dict[str, Any]) -> None:
    decisions = {item["conversation_id"]: item["decision"] for item in manifest["conversations"]}
    counts = {row["id"]: int(row["message_count"] or 0) for row in session_rows(connection)}
    explicitly_selected = {conversation_id for conversation_id, decision in decisions.items() if decision == "big_fish"}
    source_rows = connection.execute(
        """SELECT DISTINCT m.conversation_id FROM memory_sources s JOIN messages m ON m.id=s.message_id
           WHERE s.memory_id=?""", (MEMORY_ID,)
    ).fetchall()
    memory_source_conversations = {row["conversation_id"] for row in source_rows}
    assigned_memory = bool(manifest["memory"] and manifest["memory"]["decision"] == "big_fish")
    # Keep ownership components atomic: a conversation that supplies an
    # unconfirmed memory cannot be restored alone with a dangling cross-scope
    # memory source or audit trail.
    blocked_by_memory = {
        conversation_id for conversation_id in explicitly_selected & memory_source_conversations
        if not assigned_memory or decisions.get(conversation_id) != "big_fish"
    }
    selected = explicitly_selected - blocked_by_memory
    assigned_memory = assigned_memory and memory_source_conversations.issubset(selected)
    if not assigned_memory:
        selected -= memory_source_conversations
        blocked_by_memory |= explicitly_selected & memory_source_conversations
    assigned_messages = sum(counts.get(conversation_id, 0) for conversation_id in selected)
    chosen = len(decisions)
    print("\n拟恢复范围（仅报告，未写数据库）：")
    print(f"已选择 big_fish：{len(explicitly_selected)}；当前可一致恢复会话：{len(selected)}；对应消息：{assigned_messages}")
    print(f"单独确认的大肥鱼记忆：{1 if assigned_memory else 0}；其来源消息：{1 if assigned_memory else 0}")
    print(f"保持隔离/暂不能确认/跳过：{sum(1 for d in decisions.values() if d != 'big_fish')}；尚未选择：{10-chosen}")
    if blocked_by_memory:
        print("因关联记忆未独立确认而暂不恢复的会话：" + ", ".join(sorted(blocked_by_memory)))
    if selected:
        triggers = ["conversations_ownership_update"]
        if assigned_memory:
            triggers.extend(f"{table}_ownership_update" for table in (
                "memory_items", "memory_sources", "memory_change_log",
                "memory_change_proposals", "memory_proposal_change_log",
                "memory_proposal_application_log", "memory_correction_clarifications"))
        print("拟临时调整后恢复的触发器：" + ", ".join(triggers))
        print("拟执行顺序：BEGIN IMMEDIATE；核验清单与副本哈希；按确认 ID 暂停对应归属不可变触发器；")
        print("设置离线归属作用域；先更新会话，再更新经单独确认的记忆、来源和审计关联；重建触发器；")
        print("检查跨角色关联、外键、记录计数与 quick_check；全部通过才提交。任一不符即 ROLLBACK。")
    print("该清单不授权正式恢复；正式执行仍需独立备份、停服和显式维护审批。")


if __name__ == "__main__":
    raise SystemExit(main())
