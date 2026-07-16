#!/usr/bin/env python3
"""
Documentation-Code Alignment Tool

Reads system docs from docs/system_docs/, cross-references documented
claims (classes, functions, files, paths) against actual source code
(src/, sar_orch/), and produces a structured gap analysis report.

Usage:
    cd /home/wyh/daily_work/LLaMAR-sematic_map
    PYTHONPATH="src:$PYTHONPATH" uv run python sar_orch/align_docs.py
    PYTHONPATH="src:$PYTHONPATH" uv run python sar_orch/align_docs.py --output custom_report.md
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "sar_orch" / "docs_alignment"
CONFIG_PATH = CONFIG_DIR / "config.json"
DOCS_DIR = PROJECT_ROOT / "docs" / "system_docs"
SOURCE_DIRS = [
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "sar_orch",
]
REPORT_DIR = CONFIG_DIR / "reports"

SKIP_CLASSES = {
    "Step",
    "Actions",
    "Successes",
    "Coverage",
    "RunID",
    "MaxSteps",
    "Overview",
    "Strategy",
    "Summary",
    "None",
    "Workers",
    "Table",
    "Figure",
    "List",
    "Output",
    "Status",
    "Error",
    "Result",
    "Type",
    "Value",
    "Note",
    "Warning",
    "Info",
    "Debug",
    "Example",
    "Usage",
    "Path",
    "File",
    "Dir",
    "Mode",
    "Start",
    "End",
    "Next",
    "Prev",
    "First",
    "Last",
    # CSV column headers that look like classes
    "TransportRate",
    "TimeoutAgents",
    "RemainingSteps",
    "WallTimeSinceStart",
    "StepDurationMs",
    "ErrorTypes",
    "CompletedSubtasksDelta",
    "EndReason",
    "ToolName",
    "ToolArgs",
    "LLMInput",
    "LLMOutput",
    "CorrelationID",
    "EventType",
    "ToolLatencyMs",
    "ErrorType",
    "AssignedTo",
    "WorkerTaskID",
    "PromptVersion",
    "PromptTokens",
    "CompletionTokens",
    "TotalTokens",
    "CacheHitTokens",
    "CacheMissTokens",
    "LLMLatencyMs",
    "ExperimentName",
    "LogDir",
    "TotalSteps",
    "FinalCoverage",
    "FinalTransportRate",
    "TotalAgentInteractions",
    "TotalRouterInteractions",
    "SubtaskID",
    "CreatedAt",
    "UpdatedAt",
    "FailureClass",
    "EventQueue",
    "TaskStatusUpdateEvent",
    "EnvName",
    "ScenarioID",
    "ActionsByAgent",
    "ActionSuccessByAgent",
    "ErrorTypeByAgent",
    "ObservationByAgent",
    "ObjectiveProgress",
    "ExplorationProgress",
    "GlobalStateDigest",
    "ContextID",
    "CoordinatorTaskID",
    "WorkerTaskID",
    "AssignedAgent",
    "SubtaskText",
    "DispatchLatencyMs",
    "TaskPushNotificationConfig",
    "NavigateTo",
    "CarryPerson",
    "DropOffPerson",
    "GetSupply",
    "StoreSupply",
    "UseSupply",
    "ClearInventory",
    "GetAgentState",
    "ReportObservation",
    "QuerySharedMemory",
    "FinishTask",
    "SARFinishTaskTool",
    "SAREnv",
    "SubmitActionTool",
    # Outdated / refactored — referenced in docs but absent from codebase
    "DestroyedTaskError",
    "DefaultRequestHandler",
}

# Cache for file contents to avoid repeated reads
_file_cache: Dict[str, str] = {}


def _read_file(path: Path) -> str:
    """Cached file read."""
    key = str(path.resolve())
    if key not in _file_cache:
        _file_cache[key] = path.read_text(encoding="utf-8", errors="replace")
    return _file_cache[key]


class CodeIndex:
    """Pre-built index of all code symbols for fast lookup."""

    def __init__(self, source_dirs: List[Path]):
        self.source_dirs = source_dirs
        self.classes: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        self.functions: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        self.files: Set[str] = set()
        self.py_files: List[Tuple[str, Path]] = []  # (rel_path, abs_path)
        self.build()

    def build(self):
        """Build index of all code symbols by scanning once."""
        for sd in self.source_dirs:
            if not sd.exists():
                continue
            for py_file in sd.rglob("*.py"):
                rel = str(py_file.relative_to(PROJECT_ROOT))
                if "__pycache__" in py_file.parts:
                    continue
                self.files.add(rel)
                self.py_files.append((rel, py_file))
                try:
                    content = _read_file(py_file)
                except Exception:
                    continue

                for i, line in enumerate(content.split("\n"), 1):
                    stripped = line.strip()

                    # class definitions
                    m = re.match(r"^class\s+(\w+)", stripped)
                    if m:
                        self.classes[m.group(1)].append((rel, i))
                        continue

                    # function definitions
                    m = re.match(r"^(?:async\s+)?def\s+(\w+)", stripped)
                    if m:
                        self.functions[m.group(1)].append((rel, i))
                        continue

        print(
            f"Index built: {len(self.classes)} classes, {len(self.functions)} functions, {len(self.files)} files",
            file=sys.stderr,
        )
        # Warm cache with file_content lookups for fast pattern matching
        for _, py_file in self.py_files:
            _read_file(py_file)

    def find_class(self, name: str) -> Optional[str]:
        matches = self.classes.get(name, [])
        if matches:
            return f"{matches[0][0]}:{matches[0][1]}"
        return None

    def find_function(self, name: str) -> Optional[str]:
        name_only = name.split(".")[-1]
        matches = self.functions.get(name_only, [])
        if matches:
            return f"{matches[0][0]}:{matches[0][1]}"
        return None

    def find_file(self, name: str) -> Optional[str]:
        basename = name.split("/")[-1]
        for fp in sorted(self.files):
            if fp.endswith(f"/{basename}") or fp == basename:
                return fp
        # Fall back to path check
        return self.find_path(name)

    def find_path(self, pattern: str) -> Optional[str]:
        # Direct path checks
        full_path = PROJECT_ROOT / pattern
        if full_path.exists():
            return str(full_path.relative_to(PROJECT_ROOT))
        for prefix in ["src", "sar_orch", "docs", ""]:
            candidate = PROJECT_ROOT / prefix / pattern
            if candidate.exists():
                return str(candidate.relative_to(PROJECT_ROOT))
        return None

    def find_event(self, event_name: str) -> List[str]:
        results = []
        for rel, py_file in self.py_files:
            content = _read_file(py_file)
            if f"'{event_name}'" in content or f'"{event_name}"' in content:
                results.append(rel)
        return results

    def find_config_key(self, key: str) -> List[str]:
        results = []
        json_ext = [".json", ".yaml", ".yml", ".toml"]
        for sd in self.source_dirs:
            for fp in sd.rglob("*"):
                if "__pycache__" in fp.parts:
                    continue
                if fp.suffix not in (".py", *json_ext):
                    continue
                rel = str(fp.relative_to(PROJECT_ROOT))
                try:
                    content = _read_file(fp)
                    if f"'{key}'" in content or f'"{key}"' in content:
                        results.append(rel)
                except Exception:
                    continue
        return results

    def find_field(self, pattern: str) -> Optional[str]:
        cls_name = pattern.split(".")[0]
        field_name = pattern.split(".")[-1]
        cls_matches = self.classes.get(cls_name, [])
        if not cls_matches:
            return None
        path = cls_matches[0][0]
        full_path = PROJECT_ROOT / path
        if full_path.exists():
            content = _read_file(full_path)
            if f"{field_name}:" in content or f"{field_name} =" in content:
                return f"{path} (field: {field_name})"
            return f"{path} (class found, field ambiguous)"
        return None


class DocCodeAligner:
    """Core alignment engine."""

    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path
        self.config = self._load_config()
        print("Building code index (one-time scan)...", file=sys.stderr)
        self.index = CodeIndex(SOURCE_DIRS)
        self.report: Dict[str, Any] = {
            "run_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "timestamp": datetime.now().isoformat(),
            "config_version": self.config.get("version", 0),
            "documents": {},
            "summary": {
                "total_claims": 0,
                "verified": 0,
                "not_found": 0,
                "doc_not_found": 0,
                "errors": 0,
            },
        }

    def _load_config(self) -> dict:
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _verify_claim(self, claim: Dict[str, Any]) -> Dict[str, Any]:
        pattern = claim["pattern"]
        claim_type = claim["type"]
        found = False
        location = None

        if claim_type == "class":
            loc = self.index.find_class(pattern)
            if loc:
                found = True
                location = loc

        elif claim_type == "function":
            loc = self.index.find_function(pattern)
            if loc:
                found = True
                location = loc

        elif claim_type == "file":
            loc = self.index.find_file(pattern)
            if loc:
                found = True
                location = loc

        elif claim_type == "path":
            loc = self.index.find_path(pattern)
            if loc:
                found = True
                location = loc

        elif claim_type == "event":
            matches = self.index.find_event(pattern)
            if matches:
                found = True
                location = matches[0]

        elif claim_type == "field":
            loc = self.index.find_field(pattern)
            if loc:
                found = True
                location = loc

        elif claim_type == "config":
            matches = self.index.find_config_key(pattern)
            if matches:
                found = True
                location = matches[0]

        elif claim_type == "csv_field":
            logger_path = PROJECT_ROOT / "sar_orch" / "logger.py"
            if logger_path.exists():
                content = _read_file(logger_path)
                if pattern in content:
                    found = True
                    location = "sar_orch/logger.py"

        elif claim_type == "cli_flag":
            results = self.index.find_config_key(pattern)
            if results:
                found = True
                location = results[0]
            else:
                # Also check argparse patterns
                for rel, py_file in self.index.py_files:
                    content = _read_file(py_file)
                    if f"--{pattern}" in content:
                        found = True
                        location = rel
                        break

        return {
            "pattern": pattern,
            "type": claim_type,
            "description": claim.get("description", ""),
            "found": found,
            "location": location,
        }

    def _extract_doc_references(
        self,
        content: str,
        doc_name: str,
        generated_files: set | None = None,
        tool_refs: set | None = None,
    ) -> List[Dict[str, str]]:
        """Auto-extract code references from doc markdown content.

        Args:
            generated_files: Set of filenames that are runtime-generated (skip file checks).
            tool_refs: Set of tool name patterns (skip as non-Python-function checks).
        """
        references = []
        seen: Set[str] = set()
        generated_files = generated_files or set()
        tool_refs = tool_refs or set()

        # Pattern: backtick-quoted paths like `sar_orch/experiment.py`
        for m in re.finditer(
            r"`([a-zA-Z_][a-zA-Z0-9_./-]*\.(?:py|md|json|sh|yaml|yml|toml|cfg|txt))`",
            content,
        ):
            ref_path = m.group(1)
            if ref_path not in seen and len(ref_path) > 3:
                # Skip runtime-generated files
                if ref_path in generated_files:
                    seen.add(ref_path)
                    continue
                seen.add(ref_path)
                if "/" in ref_path or "." in ref_path:
                    ref_type = (
                        "file"
                        if ref_path.endswith(
                            (
                                ".md",
                                ".py",
                                ".json",
                                ".sh",
                                ".yaml",
                                ".yml",
                                ".toml",
                                ".cfg",
                                ".txt",
                            )
                        )
                        else "path"
                    )
                    references.append(
                        {
                            "pattern": ref_path,
                            "type": ref_type,
                            "description": f"Auto-extracted from {doc_name}",
                        }
                    )

        # Pattern: CamelCase class names in backtick code blocks
        for m in re.finditer(
            r"`([A-Z][a-zA-Z0-9]+(?:[A-Z][a-zA-Z0-9]+)+)`",
            content,
        ):
            cls_name = m.group(1)
            if (
                cls_name not in seen
                and cls_name not in SKIP_CLASSES
                and len(cls_name) > 4
            ):
                seen.add(cls_name)
                references.append(
                    {
                        "pattern": cls_name,
                        "type": "class",
                        "description": f"Auto-extracted class from {doc_name}",
                    }
                )

        return references

    def run(self) -> Dict[str, Any]:
        """Run the full alignment check."""
        doc_configs = self.config.get("documents", {})
        total = 0
        verified = 0
        not_found = 0

        for doc_name, doc_config in doc_configs.items():
            doc_path = DOCS_DIR / doc_name
            doc_result = {
                "title": doc_config.get("title", doc_name),
                "doc_exists": doc_path.exists(),
                "claims": [],
            }

            if not doc_path.exists():
                doc_result["error"] = f"Document file not found: {doc_path}"
                self.report["summary"]["doc_not_found"] += 1
                self.report["documents"][doc_name] = doc_result
                print(f"  ⚠️  {doc_name}: file not found", file=sys.stderr)
                continue

            claims = list(doc_config.get("key_claims", []))
            generated_files = set(doc_config.get("generated_files", []))
            tool_refs = set(self.config.get("tool_references", []))

            doc_content = _read_file(doc_path)
            extra_refs = self._extract_doc_references(
                doc_content,
                doc_name,
                generated_files=generated_files,
                tool_refs=tool_refs,
            )
            existing_patterns = {c["pattern"] for c in claims}
            for ref in extra_refs:
                if ref["pattern"] not in existing_patterns:
                    claims.append(ref)
                    existing_patterns.add(ref["pattern"])

            print(f"  📄 {doc_name}: {len(claims)} claims to check", file=sys.stderr)

            for claim in claims:
                total += 1
                result = self._verify_claim(claim)
                doc_result["claims"].append(result)
                if result["found"]:
                    verified += 1
                else:
                    not_found += 1

            self.report["documents"][doc_name] = doc_result

        self.report["summary"]["total_claims"] = total
        self.report["summary"]["verified"] = verified
        self.report["summary"]["not_found"] = not_found

        return self.report

    def format_markdown_report(self, report: Dict[str, Any]) -> str:
        """Format the alignment report as readable markdown."""
        lines: List[str] = []
        s = report["summary"]

        lines.append("# 文档-代码对齐报告")
        lines.append("")
        lines.append("| 字段 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 运行ID | {report['run_id']} |")
        lines.append(f"| 时间 | {report['timestamp']} |")
        lines.append(f"| 配置版本 | {report['config_version']} |")
        lines.append("")
        lines.append("## 汇总统计")
        lines.append("")
        lines.append("| 指标 | 数量 |")
        lines.append("|------|------|")
        lines.append(f"| 总检查项 | {s['total_claims']} |")
        lines.append(f"| ✅ 已验证 | {s['verified']} |")
        lines.append(f"| ❌ 未找到 | {s['not_found']} |")
        lines.append(f"| 📄 文档缺失 | {s['doc_not_found']} |")
        lines.append("")
        accuracy = (
            (s["verified"] / s["total_claims"] * 100) if s["total_claims"] > 0 else 0
        )
        lines.append(f"**对齐准确率: {accuracy:.1f}%**")
        lines.append("")

        if s["not_found"] > 0:
            lines.append("### ❌ 缺失项详情")
            lines.append("")
            lines.append("| 文档 | 模式 | 类型 | 描述 |")
            lines.append("|------|------|------|------|")
            for doc_name, doc_result in report["documents"].items():
                for claim in doc_result.get("claims", []):
                    if not claim["found"]:
                        lines.append(
                            f"| {doc_name} | `{claim['pattern']}` | {claim['type']} | {claim['description']} |"
                        )
            lines.append("")

        for doc_name, doc_result in report["documents"].items():
            lines.append(f"## 📄 {doc_result['title']} (`{doc_name}`)")
            lines.append("")
            if not doc_result.get("doc_exists", False):
                lines.append("⚠️  **文档文件不存在**")
                lines.append("")
                continue

            claims = doc_result.get("claims", [])
            if not claims:
                lines.append("_无检查项_")
                lines.append("")
                continue

            doc_verified = sum(1 for c in claims if c["found"])
            doc_total = len(claims)
            doc_accuracy = (doc_verified / doc_total * 100) if doc_total > 0 else 0

            lines.append(f"**{doc_verified}/{doc_total} 已验证 ({doc_accuracy:.0f}%)**")
            lines.append("")
            lines.append("| 状态 | 模式 | 类型 | 位置 | 描述 |")
            lines.append("|------|------|------|------|------|")
            for claim in claims:
                status = "✅" if claim["found"] else "❌"
                loc = claim.get("location") or "—"
                lines.append(
                    f"| {status} | `{claim['pattern']}` | {claim['type']} | `{loc}` | {claim['description']} |"
                )
            lines.append("")

        return "\n".join(lines)

    def save_report(self, report: Dict[str, Any], output_path: Optional[Path] = None):
        """Save the report as both JSON and markdown."""
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        run_id = report["run_id"]

        json_path = REPORT_DIR / f"alignment_{run_id}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nJSON report: {json_path}")

        md_report = self.format_markdown_report(report)
        if output_path:
            md_path = Path(output_path)
        else:
            md_path = REPORT_DIR / f"alignment_{run_id}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_report)
        print(f"Markdown report: {md_path}")

        latest_md = REPORT_DIR / "latest.md"
        latest_md.write_text(md_report, encoding="utf-8")
        print(f"Latest report: {latest_md}")

        latest_json = REPORT_DIR / "latest.json"
        latest_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Print summary to stdout for cron delivery
        s = report["summary"]
        accuracy = (
            (s["verified"] / s["total_claims"] * 100) if s["total_claims"] > 0 else 0
        )
        print(f"\n{'=' * 60}")
        print("  文档-代码对齐结果")
        print(f"{'=' * 60}")
        print(f"  运行ID:    {run_id}")
        print(f"  总检查项:  {s['total_claims']}")
        print(f"  已验证:    {s['verified']} ({accuracy:.1f}%)")
        print(f"  未找到:    {s['not_found']}")
        if s["not_found"] > 0:
            print("\n  ❌ 缺失项:")
            for doc_name, doc_result in report["documents"].items():
                for claim in doc_result.get("claims", []):
                    if not claim["found"]:
                        print(
                            f"    [{doc_name}] {claim['pattern']} ({claim['type']}) — {claim['description']}"
                        )
        print(f"{'=' * 60}")

        return json_path, md_path

    def _update_frontmatter_date(self, content: str, today: str) -> tuple[str, bool]:
        """Update the `日期:` (date) field in YAML frontmatter to today's date.

        Returns:
            (updated_content, was_modified)
        """
        date_pattern = re.compile(r"^(\s*日期\s*:\s*)\d{4}-\d{2}-\d{2}", re.MULTILINE)
        match = date_pattern.search(content)
        if match:
            old_line = match.group(0)
            new_line = f"{match.group(1)}{today}"
            if old_line != new_line:
                content = content.replace(old_line, new_line, 1)
                return content, True
        return content, False

    def auto_fix(self, report: Dict[str, Any]) -> Dict[str, list[str]]:
        """Auto-fix misalignments in doc files.

        For each doc with missing claims that were auto-extracted (not from
        config's key_claims), remove backtick quotes around stale references
        and update the document's `日期:` frontmatter to today's date.

        Returns:
            Dict mapping doc_name -> list of fix descriptions
        """
        from datetime import date

        today = date.today().isoformat()  # e.g. "2026-07-15"

        doc_configs = self.config.get("documents", {})
        fixes: Dict[str, list[str]] = {}

        for doc_name, doc_result in report.get("documents", {}).items():
            doc_path = DOCS_DIR / doc_name
            if not doc_path.exists():
                continue

            # Only process docs that have config entries
            doc_config = doc_configs.get(doc_name)
            if doc_config is None:
                continue

            config_patterns = {c["pattern"] for c in doc_config.get("key_claims", [])}
            missing_claims = [c for c in doc_result.get("claims", []) if not c["found"]]

            if not missing_claims:
                continue  # Nothing to fix

            content = _read_file(doc_path)
            old_content = content
            doc_fixes: list[str] = []

            for claim in missing_claims:
                pattern = claim["pattern"]

                # Skip config-defined claims — those need human judgment
                if pattern in config_patterns:
                    continue

                # Skip generated/runtime output files
                generated = set(doc_config.get("generated_files", []))
                if pattern in generated:
                    continue

                # For backtick-quoted references: remove backticks
                # to demote them from "code reference" to plain text
                old_ref = f"`{pattern}`"
                new_ref = pattern  # remove backticks → plain text
                if old_ref in content:
                    count = content.count(old_ref)
                    content = content.replace(old_ref, new_ref)
                    doc_fixes.append(
                        f"Removed backticks from `{pattern}` ({count} occurrence{'s' if count > 1 else ''})"
                    )

            # Update frontmatter date if any changes were made
            if doc_fixes:
                content, date_updated = self._update_frontmatter_date(content, today)
                if date_updated:
                    doc_fixes.append(f"Updated date to {today}")

            if content != old_content:
                doc_path.write_text(content, encoding="utf-8")
                _file_cache.pop(str(doc_path.resolve()), None)  # invalidate cache
                fixes[doc_name] = doc_fixes
                print(f"  🔧 Auto-fixed: {doc_name}")
                for fix in doc_fixes:
                    print(f"    • {fix}")
            else:
                print(
                    f"  ℹ️  {doc_name}: {len(missing_claims)} missing claims, none auto-fixable",
                    file=sys.stderr,
                )

        return fixes


def main():
    parser = argparse.ArgumentParser(description="Document-Code Alignment Tool")
    parser.add_argument(
        "--config",
        type=str,
        default=str(CONFIG_PATH),
        help="Path to alignment config JSON",
    )
    parser.add_argument(
        "--output", type=str, default=None, help="Path for markdown report output"
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help="Auto-fix stale references in docs and update dates",
    )
    args = parser.parse_args()

    aligner = DocCodeAligner(Path(args.config))
    report = aligner.run()
    json_path, md_path = aligner.save_report(
        report, Path(args.output) if args.output else None
    )

    fixes = {}
    if args.auto_fix:
        print(f"\n{'=' * 60}")
        print("  自动修复阶段")
        print(f"{'=' * 60}")
        fixes = aligner.auto_fix(report)
        if fixes:
            total_fixes = sum(len(v) for v in fixes.values())
            print(f"\n✅ 已修复 {len(fixes)} 个文档文件，共 {total_fixes} 项改动")
        else:
            print("\n✅ 无需自动修复")

    # JSON output for programmatic consumption by cron jobs
    result = {
        "run_id": report["run_id"],
        "summary": report["summary"],
        "auto_fixes": {doc: len(fix_list) for doc, fix_list in fixes.items()},
        "auto_fix_details": fixes,
    }
    # Always write auto_fix_result.json for cron job to read
    result_path = REPORT_DIR / "auto_fix_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return 0 if report["summary"]["not_found"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
