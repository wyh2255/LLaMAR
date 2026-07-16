#!/usr/bin/env python3
"""
Send message to Feishu group via custom bot webhook.
This bot only supports text messages (not interactive cards).

Usage:
    python3 send_webhook.py "text message"
    python3 send_webhook.py --template alignment <accuracy> <missing>
    python3 send_webhook.py --template evolution <accuracy> <gaps> <updated>
"""

import json
import sys
import urllib.request
from typing import Optional

WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/9b4d8bec-e5f7-4b5d-8f0b-be567e1f75a8"


def send_text(text: str) -> dict:
    payload = {
        "msg_type": "text",
        "content": {"text": text},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def make_alignment_text(run_id: str, total: int, verified: int,
                         not_found: int, accuracy: float,
                         missing_items: list[str]) -> str:
    lines = [
        "📋 文档-代码对齐日报",
        "=" * 30,
        f"运行ID: {run_id}",
        "",
        "📊 汇总",
        f"  总检查项: {total}",
        f"  ✅ 已验证: {verified}",
        f"  ❌ 未找到: {not_found}",
        f"  📈 准确率: {accuracy:.1f}%",
    ]
    if missing_items:
        lines.extend([
            "",
            f"⚠️ 缺失项 ({len(missing_items)})",
        ])
        for item in missing_items[:10]:
            lines.append(f"  • {item}")
        if len(missing_items) > 10:
            lines.append(f"  ... 及另外 {len(missing_items) - 10} 项")
    else:
        lines.extend(["", "✅ 无缺失项"])
    return "\n".join(lines)


def make_evolution_text(timestamp: str, accuracy_before: Optional[float],
                         total_gaps: int, true_gaps: int,
                         config_updated: bool,
                         auto_fixes: dict) -> str:
    lines = [
        "🔄 对齐 Prompt 进化日报",
        "=" * 30,
    ]
    if accuracy_before is not None:
        lines.append(f"之前准确率: {accuracy_before:.1f}%")
    lines.append(f"总缺失项: {total_gaps}")
    lines.append(f"真实缺口: {true_gaps}")
    lines.append("")

    if config_updated:
        lines.append("✅ 自动改进:")
        for k, v in auto_fixes.items():
            if v:
                label = {
                    "output_files_marked": "标记生成文件",
                    "placeholders_ignored": "忽略占位路径",
                    "tools_marked": "标记工具引用",
                }.get(k, k)
                lines.append(f"  • {label}: {v} 项")
    else:
        lines.append("ℹ️ 无需更新配置")

    lines.append("")
    lines.append("配置: sar_orch/docs_alignment/config.json")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        text = sys.stdin.read().strip()
        if text:
            return send_text(text)
        print("Usage: python3 send_webhook.py <text>", file=sys.stderr)
        print("  or:  python3 send_webhook.py --template alignment ...", file=sys.stderr)
        print("  or:  python3 send_webhook.py --template evolution ...", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--template" and len(sys.argv) >= 4:
        tpl = sys.argv[2]
        if tpl == "alignment":
            args = sys.argv[3:]
            run_id = args[0] if len(args) > 0 else "unknown"
            total = int(args[1]) if len(args) > 1 else 0
            verified = int(args[2]) if len(args) > 2 else 0
            not_found = int(args[3]) if len(args) > 3 else 0
            accuracy = float(args[4]) if len(args) > 4 else 0.0
            missing = args[5].split("|") if len(args) > 5 and args[5] else []
            text = make_alignment_text(run_id, total, verified, not_found, accuracy, missing)
            return send_text(text)
        elif tpl == "evolution":
            args = sys.argv[3:]
            timestamp = args[0] if len(args) > 0 else ""
            accuracy_before = float(args[1]) if len(args) > 1 and args[1] != "None" else None
            total_gaps = int(args[2]) if len(args) > 2 else 0
            true_gaps = int(args[3]) if len(args) > 3 else 0
            config_updated = args[4].lower() == "true" if len(args) > 4 else False
            auto_fixes = json.loads(args[5]) if len(args) > 5 else {}
            text = make_evolution_text(timestamp, accuracy_before, total_gaps, true_gaps, config_updated, auto_fixes)
            return send_text(text)

    text = " ".join(sys.argv[1:])
    return send_text(text)


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False))
