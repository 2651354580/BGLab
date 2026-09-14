""

# Import bundled skill modules — side effect: register_bundled_skill() runs.
from bglab.skills import simplify as _simplify           # noqa: F401
from bglab.skills import stuck as _stuck                  # noqa: F401
from bglab.skills import debug as _debug                  # noqa: F401
from bglab.skills import code_review as _code_review     # noqa: F401
from bglab.skills import security_review as _security    # noqa: F401

from bglab.skills.loader import load_skills, get_skill_attachment
from bglab.skills.base import get_skill, get_bundled_skills

__all__ = [
    "load_skills",
    "get_skill_attachment",
    "get_skill",
    "get_bundled_skills",
]