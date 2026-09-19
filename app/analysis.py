"""Unicode 规范化与脚本（script）分析。

- 规范名：NFKC 归一化后再做 casefold，用于大小写/兼容字符无关的相等性比较。
- 脚本分析：基于 unicodedataplus 的 script()，忽略 Common 与 Inherited，
  统计名称中出现的其余脚本及对应码点；当 Latin / Cyrillic / Greek
  三个高风险相似脚本中同时出现两种及以上时，判定为混脚本违规。
"""
from __future__ import annotations

import unicodedataplus as udp

# 标点、数字、空白、组合符等不参与混脚本判定
IGNORED_SCRIPTS = frozenset({"Common", "Inherited"})

# 互似字形最多的三个脚本，混用即违规
RESTRICTED_SCRIPTS = frozenset({"Latin", "Cyrillic", "Greek"})


def normalize_name(name: str) -> str:
    """计算规范名：NFKC + casefold。"""
    return udp.normalize("NFKC", name).casefold()


def analyze_scripts(name: str) -> dict:
    """按 Unicode 脚本分组名称中的字符（忽略 Common / Inherited）。

    返回脚本清单、每个脚本包含的字符及其码点，以及混脚本违规判定。
    """
    by_script: dict[str, list[str]] = {}
    for char in name:
        script = udp.script(char)
        if script in IGNORED_SCRIPTS:
            continue
        chars = by_script.setdefault(script, [])
        if char not in chars:
            chars.append(char)

    script_details = [
        {
            "script": script,
            "characters": chars,
            "code_points": [f"U+{ord(char):04X}" for char in chars],
        }
        for script, chars in sorted(by_script.items())
    ]
    restricted_used = sorted(set(by_script) & RESTRICTED_SCRIPTS)
    return {
        "scripts": [detail["script"] for detail in script_details],
        "script_details": script_details,
        "restricted_scripts_used": restricted_used,
        "mixed_script_violation": len(restricted_used) >= 2,
    }
