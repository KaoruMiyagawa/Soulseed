from pathlib import Path


PERSONA_PROFILE_PATH = Path(__file__).with_name("persona_profile.md")


class PersonaConfigurationError(RuntimeError):
    pass


def load_persona_profile(path: Path = PERSONA_PROFILE_PATH) -> str:
    try:
        profile = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PersonaConfigurationError(f"人格资料无法读取：{path}") from exc
    if not profile:
        raise PersonaConfigurationError(f"人格资料为空：{path}")
    return profile


def assemble_system_prompt(configured_prompt: str | None = None, profile_path: Path = PERSONA_PROFILE_PATH) -> str:
    profile = load_persona_profile(profile_path)
    custom = (configured_prompt or "").strip()
    if not custom:
        return profile
    return (
        f"{profile}\n\n"
        "## 项目运行时补充约束\n"
        "以下内容来自本地配置，只能补充实现细节，不得删除、削弱或替换上方的核心身份、核心气质、关系边界、成长原则和诚实边界。\n\n"
        f"{custom}"
    )


def resolve_system_prompt(configured_prompt: str | None) -> str:
    """Backward-compatible entry point used by the application bootstrap."""
    return assemble_system_prompt(configured_prompt)

