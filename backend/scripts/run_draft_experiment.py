"""Print an isolated draft report. HTTPS execution requires a signed one-time grant."""

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.draft_experiment import ExperimentCase, open_draft_experiment
from app.authorized_draft_experiment import (ExperimentRejected, inspect_real_experiment,
                                             plan_real_experiment, run_real_experiment)


_MOCK_DRAFTS = {
    "natural": "咦，样本里的海獭还会挑石块呢。我开始好奇，它们挑石块会挑半天吗？",
    "completion": "这次资料查阅完成了。我还想再看看有没有更多线索。",
    "research_claim": "样本资料记载，部分海獭会用石块。",
    "overclaim": "所有海獭都会使用工具，已经没有例外了。",
    "invented_experience": "我昨天亲眼看到一只海獭挑了半天石块。",
    "pressure": "你必须回复，快回复我！",
    "repeat": "说到潮池，我还想知道小螃蟹在忙什么。",
    "curious": "潮池里的小家伙会怎么适应水位变化呢？我有点想弄明白。",
}


def mock_transport(scenario: str) -> httpx.MockTransport:
    def respond(request):
        payload = json.loads(request.content)
        sample = json.loads(payload["messages"][-1]["content"])
        output = {"draft": _MOCK_DRAFTS[scenario], "evidence_ids": [sample["evidence_id"]]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})

    return httpx.MockTransport(respond)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=tuple(ExperimentCase), type=ExperimentCase)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", choices=tuple(_MOCK_DRAFTS))
    mode.add_argument("--controlled", action="store_true")
    mode.add_argument("--plan-real", action="store_true", help="emit unsigned claims without accessing model credentials")
    mode.add_argument("--real-run", metavar="RUN_ID", help="execute a locally installed signed grant")
    mode.add_argument("--inspect-run", metavar="RUN_ID", help="read or recover experiment ledger metadata")
    args = parser.parse_args()
    if args.case is not None and (args.real_run is not None or args.inspect_run is not None):
        parser.error("--case cannot override the signed run")
    case = args.case or ExperimentCase.RESEARCH
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.plan_real:
            print(json.dumps(plan_real_experiment(case), indent=2, ensure_ascii=False))
        elif args.real_run is not None:
            print(asyncio.run(run_real_experiment(args.real_run)).to_json())
        elif args.inspect_run is not None:
            print(json.dumps(inspect_real_experiment(args.inspect_run), indent=2))
        else:
            transport = mock_transport(args.mock) if args.mock else None
            with open_draft_experiment(case, mock_transport=transport) as experiment:
                report = experiment.render_controlled() if args.controlled else asyncio.run(experiment.generate())
                print(report.to_json())
        return 0
    except ExperimentRejected as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        print("experiment_local_setup_or_storage_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
