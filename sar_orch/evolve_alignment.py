#!/usr/bin/env python3
"""
Alignment Config Evolution Tool

Reads the latest alignment report and suggests improvements to the alignment
configuration (config.json). This is the engine used by the 4AM evolution job.

Usage:
    cd /home/wyh/daily_work/LLaMAR-sematic_map
    PYTHONPATH="src:$PYTHONPATH" python3 sar_orch/evolve_alignment.py

Output:
    - Updates sar_orch/docs_alignment/config.json with improved patterns
    - Saves evolution_log entries
    - Reports summary to stdout
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "sar_orch" / "docs_alignment"
CONFIG_PATH = CONFIG_DIR / "config.json"
REPORT_PATH = CONFIG_DIR / "reports" / "latest.json"
EVOLUTION_LOG_PATH = CONFIG_DIR / "evolution_log.json"


class AlignmentEvolver:
    """Analyzes alignment results and evolves the config."""

    def __init__(self):
        self.config = self._load_json(CONFIG_PATH)
        self.report = self._load_json(REPORT_PATH) if REPORT_PATH.exists() else None
        self.evolution_log = self._load_json(EVOLUTION_LOG_PATH) if EVOLUTION_LOG_PATH.exists() else []

    def _load_json(self, path: Path) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_json(self, path: Path, data: Any):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def analyze_gaps(self) -> Dict[str, List[str]]:
        """Analyze missing items and categorize them."""
        if not self.report:
            return {}

        categories = {
            "output_files": [],
            "outdated_refs": [],
            "tool_names": [],
            "placeholder_paths": [],
            "true_gaps": [],
        }

        for doc_name, doc_result in self.report.get("documents", {}).items():
            for claim in doc_result.get("claims", []):
                if claim.get("found"):
                    continue

                pattern = claim["pattern"]
                ctype = claim["type"]
                desc = claim.get("description", "")

                # Categorize the gap
                if ctype == "file" and pattern.endswith((".csv", ".ndjson", ".jsonl")):
                    categories["output_files"].append(f"[{doc_name}] {pattern}")
                elif "results/benchmark/..." in pattern:
                    categories["placeholder_paths"].append(f"[{doc_name}] {pattern}")
                elif ctype == "file" and pattern.endswith(".json"):
                    categories["output_files"].append(f"[{doc_name}] {pattern}")
                elif ctype == "function" and pattern in ("read_file", "write_file", "edit_file"):
                    categories["tool_names"].append(f"[{doc_name}] {pattern}")
                elif ctype == "event" or "Event" in pattern:
                    categories["outdated_refs"].append(f"[{doc_name}] {pattern}")
                elif ctype == "class" and pattern == "DestroyedTaskError":
                    categories["outdated_refs"].append(f"[{doc_name}] {pattern}")
                elif ctype == "function" and "DefaultRequestHandler" in pattern:
                    categories["outdated_refs"].append(f"[{doc_name}] {pattern}")
                elif ctype == "class" and pattern == "SubmitActionTool":
                    categories["outdated_refs"].append(f"[{doc_name}] {pattern}")
                else:
                    categories["true_gaps"].append(f"[{doc_name}] {pattern} ({ctype})")

        return categories

    def suggest_config_updates(self, categories: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        """Generate suggested config improvements."""
        suggestions = []

        # If output files are consistently missing, add them to the config as known generated files
        if categories.get("output_files"):
            suggestions.append({
                "type": "add_to_skip",
                "detail": "Generated output files should be marked as known-generated (not source code)",
                "patterns": categories["output_files"],
            })

        # If tool names are being flagged, they should be listed as known tools
        if categories.get("tool_names"):
            suggestions.append({
                "type": "mark_as_tool",
                "detail": "Tool names should be identified as non-Python-function references",
                "patterns": categories["tool_names"],
            })

        # True gaps should trigger config additions
        if categories.get("true_gaps"):
            suggestions.append({
                "type": "add_claim",
                "detail": "Truly missing items need config claims added or doc updated",
                "patterns": categories["true_gaps"],
            })

        return suggestions

    def run(self) -> Dict[str, Any]:
        """Run the evolution analysis."""
        result = {
            "timestamp": datetime.now().isoformat(),
            "config_version_before": self.config.get("version", 0),
            "report_accuracy": self.report.get("summary", {}).get("verified", 0) / max(self.report.get("summary", {}).get("total_claims", 1), 1) * 100 if self.report else None,
            "categories": {},
            "suggestions": [],
            "config_updated": False,
        }

        if not self.report:
            print("⚠️  No alignment report found. Run align_docs.py first.", file=sys.stderr)
            return result

        categories = self.analyze_gaps()
        result["categories"] = categories

        total_gaps = sum(len(v) for v in categories.values())
        true_gaps = len(categories.get("true_gaps", []))

        print(f"📊 分析报告总缺失项: {total_gaps}")
        print(f"   - 输出文件(非源码): {len(categories.get('output_files', []))}")
        print(f"   - 过期引用: {len(categories.get('outdated_refs', []))}")
        print(f"   - 工具名引用: {len(categories.get('tool_names', []))}")
        print(f"   - 占位路径: {len(categories.get('placeholder_paths', []))}")
        print(f"   - 真实缺口: {true_gaps}")

        # Try to auto-fix by marking generated output files
        if categories.get("output_files"):
            self._add_generated_files_to_config(categories["output_files"])
            result["config_updated"] = True
            print(f"  ✅ 自动更新 config: 标记 {len(categories['output_files'])} 个生成文件")

        if categories.get("placeholder_paths"):
            self._add_placeholder_skip(categories["placeholder_paths"])
            result["config_updated"] = True
            print(f"  ✅ 自动更新 config: 忽略 {len(categories['placeholder_paths'])} 个占位路径")

        if categories.get("tool_names"):
            self._add_tool_references(categories["tool_names"])
            result["config_updated"] = True
            print(f"  ✅ 自动更新 config: 标记 {len(categories['tool_names'])} 个工具引用")

        # Remove outdated references from key_claims
        outdated_removed = self._remove_outdated_claims(categories.get("outdated_refs", []))
        if outdated_removed:
            result["config_updated"] = True
            print(f"  ✅ 自动更新 config: 移除 {outdated_removed} 个过期 claims")

        # Record evolution
        entry = {
            "timestamp": result["timestamp"],
            "accuracy_before": round(result["report_accuracy"], 1) if result["report_accuracy"] else None,
            "gaps_found": total_gaps,
            "true_gaps": true_gaps,
            "config_updated": result["config_updated"],
            "auto_fixes": {
                "output_files_marked": len(categories.get("output_files", [])),
                "placeholders_ignored": len(categories.get("placeholder_paths", [])),
                "tools_marked": len(categories.get("tool_names", [])),
                "outdated_removed": outdated_removed,
            },
        }
        self.evolution_log.append(entry)
        self._save_json(EVOLUTION_LOG_PATH, self.evolution_log)

        if result["config_updated"]:
            # Append history entry BEFORE saving
            self.config.setdefault("evolution_history", []).append({
                "timestamp": result["timestamp"],
                "changes": "Auto-evolved from alignment report analysis",
            })
            self._save_json(CONFIG_PATH, self.config)

        return result

    def _add_generated_files_to_config(self, items: List[str]):
        """Mark generated/output files as known non-source items, appending to existing list."""
        patterns_by_doc: Dict[str, set] = {}
        for item in items:
            m = re.match(r'\[(.+?)\]\s+(.+)', item)
            if m:
                doc_name = m.group(1)
                patterns_by_doc.setdefault(doc_name, set()).add(m.group(2))

        for doc_name, patterns in patterns_by_doc.items():
            doc_config = self.config.setdefault("documents", {}).get(doc_name)
            if doc_config:
                existing = set(doc_config.get("generated_files", []))
                new_patterns = patterns - existing
                if new_patterns:
                    doc_config["generated_files"] = list(existing | new_patterns)
                    print(f"  📝 Added {len(new_patterns)} generated files to [{doc_name}]")

    def _add_placeholder_skip(self, items: List[str]):
        """Add placeholder path skip patterns."""
        if "skip_patterns" not in self.config:
            self.config["skip_patterns"] = []
        patterns = set()
        for item in items:
            m = re.match(r'\[(.+?)\]\s+(.+)', item)
            if m:
                patterns.add(m.group(2))
        for p in patterns:
            if p not in self.config["skip_patterns"]:
                self.config["skip_patterns"].append(p)

    def _add_tool_references(self, items: List[str]):
        """Mark tool name references as known."""
        if "tool_references" not in self.config:
            self.config["tool_references"] = []
        patterns = set()
        for item in items:
            m = re.match(r'\[(.+?)\]\s+(.+)', item)
            if m:
                patterns.add(m.group(2))
        for p in patterns:
            if p not in self.config["tool_references"]:
                self.config["tool_references"].append(p)

    def _remove_outdated_claims(self, items: List[str]) -> int:
        """Remove patterns from key_claims that are known outdated references.

        Parses items in the form '[doc_name] pattern' (or '[doc_name] pattern (type)')
        and removes matching entries from the document's key_claims list.

        Returns:
            Number of claims removed across all documents.
        """
        if not items:
            return 0
        removals_by_doc: Dict[str, set] = {}
        for item in items:
            # Try with type suffix first: [doc_name] pattern (type)
            m = re.match(r'\[(.+?)\]\s+(.+?)\s*\((\w+)\)', item)
            if m:
                doc_name = m.group(1)
                removals_by_doc.setdefault(doc_name, set()).add(m.group(2))
            else:
                # Fallback: [doc_name] pattern (no type)
                m = re.match(r'\[(.+?)\]\s+(.+)', item)
                if m:
                    doc_name = m.group(1)
                    removals_by_doc.setdefault(doc_name, set()).add(m.group(2))

        total_removed = 0
        for doc_name, patterns in removals_by_doc.items():
            doc_config = self.config.setdefault("documents", {}).get(doc_name)
            if not doc_config:
                continue
            old_claims = doc_config.get("key_claims", [])
            before = len(old_claims)
            new_claims = [c for c in old_claims if c.get("pattern") not in patterns]
            removed = before - len(new_claims)
            if removed > 0:
                doc_config["key_claims"] = new_claims
                total_removed += removed
                print(f"  🗑️ 从 [{doc_name}] 移除了 {removed} 个过期 claims")
        return total_removed


def main():
    evolver = AlignmentEvolver()
    result = evolver.run()

    print(f"\n{'='*60}")
    print(f"  对齐配置进化结果")
    print(f"{'='*60}")
    print(f"  时间:        {result['timestamp']}")
    print(f"  准确率进化前: {result.get('report_accuracy', 'N/A')}%")
    print(f"  真实缺口:    {result.get('categories', {}).get('true_gaps', [])}")
    print(f"  配置更新:    {'是 ✅' if result['config_updated'] else '否'}")
    print(f"{'='*60}")

    # Print true gaps for user attention
    true_gaps = result.get("categories", {}).get("true_gaps", [])
    if true_gaps:
        print(f"\n⚠️  以下真实缺口需要人工关注:")
        for gap in true_gaps:
            print(f"   - {gap}")

    return 0 if not true_gaps else 1


if __name__ == "__main__":
    sys.exit(main())
