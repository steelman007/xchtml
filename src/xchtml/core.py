import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path


def load_database_test_results(db_path):
    """Loads test results from the SQLite database in an xcresult bundle."""
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found.")
        return None
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Query test suites and their runs and locate any attached JSON metadata payload.
        cursor.execute("""
            SELECT
                ts.name as suite_name,
                tc.name as test_name,
                tcr.result as status,
                tcr.duration as duration,
                MAX(att.xcResultKitPayloadRefId) as payload_ref
            FROM TestCaseRuns tcr
            JOIN TestCases tc ON tcr.testCase_fk = tc.rowid
            JOIN TestSuites ts ON tc.testSuite_fk = ts.rowid
            LEFT JOIN Activities a ON a.testCaseRun_fk = tcr.rowid
            LEFT JOIN Attachments att ON att.activity_fk = a.rowid
                AND att.name = 'Test Metadata'
                AND att.uniformTypeIdentifier = 'public.json'
            GROUP BY tcr.rowid
            ORDER BY ts.name, tc.name
        """)
        
        results = cursor.fetchall()
        conn.close()
        
        return results if results else []
    
    except Exception as e:
        print(f"Error reading database: {e}")
        return None


def load_attachment_metadata(bundle_path, payload_ref):
    """Loads JSON metadata from an xcresult attachment payload ID."""
    if not payload_ref or not bundle_path or not os.path.isdir(bundle_path):
        return {}
    
    try:
        result = subprocess.run(
            [
                'xcrun',
                'xcresulttool',
                'get',
                'object',
                '--legacy',
                '--path',
                bundle_path,
                '--id',
                payload_ref,
                '--format',
                'raw',
            ],
            capture_output=True,
            text=False,
            check=False,
        )

        if result.returncode != 0:
            return {}

        raw_bytes = result.stdout
        if not raw_bytes:
            return {}

        try:
            return json.loads(raw_bytes.decode('utf-8'))
        except Exception:
            text = raw_bytes.decode('utf-8', errors='replace')
            start = text.find('{')
            end = text.rfind('}')
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except Exception:
                    return {}

    except Exception:
        return {}

    return {}


def normalize_requirements(requirements):
    if requirements is None:
        return []
    if isinstance(requirements, str):
        r = requirements.strip()
        if not r:
            return []
        if r.upper() in ('SRS_ID', 'SRS-ID', 'SRSID'):
            return []
        return [r]
    normalized = []
    if isinstance(requirements, dict) and '_value' in requirements:
        return normalize_requirements(requirements['_value'])
    if isinstance(requirements, list):
        for req in requirements:
            if isinstance(req, dict) and '_value' in req:
                req = req['_value']
            if isinstance(req, str) and req.strip():
                r = req.strip()
                if r.upper() in ('SRS_ID', 'SRS-ID', 'SRSID'):
                    continue
                normalized.append(r)
    return normalized


def parse_srs_id_from_metadata(metadata, default_id):
    if not isinstance(metadata, dict):
        return default_id
    
    # Prefer explicit SRS fields when they contain a concrete identifier
    for key in ('srs_id', 'SRS_ID', 'srsId'):
        val = metadata.get(key)
        if isinstance(val, str):
            v = val.strip()
            if v and v.upper() not in ('SRS_ID', 'SRS-ID', 'SRSID'):
                return v

    # Inspect requirements for likely SRS identifiers. Prefer values that contain
    # digits or a dash (e.g. APP-1235) and skip generic placeholders like "SRS_ID".
    requirements = normalize_requirements(metadata.get('requirements'))
    candidates = []
    for req in requirements:
        if not isinstance(req, str):
            continue
        r = req.strip()
        if not r:
            continue
        ru = r.upper()
        if ru in ('SRS_ID', 'SRS-ID', 'SRSID'):
            continue
        # Heuristic: prefer identifiers that contain digits or a hyphen
        if any(ch.isdigit() for ch in r) or '-' in r:
            return r
        candidates.append(r)

    # Fallback to any candidate starting with SRS/REQ (excluding placeholders)
    for c in candidates:
        cu = c.upper()
        if cu.startswith('SRS') or cu.startswith('REQ'):
            return c

    return default_id


def extract_metadata_from_node(node):
    if not isinstance(node, dict):
        return {}

    metadata = {}
    
    if isinstance(node.get('metadata'), dict):
        metadata = node['metadata']
        if metadata:
            return metadata

    attachments = node.get('attachments')
    if isinstance(attachments, dict) and '_values' in attachments:
        attachments = attachments['_values']

    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            real_name = str(attachment.get('name', '')).lower()
            uri = str(attachment.get('uniformTypeIdentifier', '')).lower()
            if 'test metadata' in real_name or 'json' in uri or str(attachment.get('filenameOverride', '')).lower().endswith('.json'):
                payload = attachment.get('payload') or attachment.get('content') or attachment.get('string') or attachment.get('data')
                if isinstance(payload, dict):
                    return payload
                if isinstance(payload, str):
                    try:
                        return json.loads(payload)
                    except Exception:
                        pass
    
    for value in node.values():
        if isinstance(value, dict):
            metadata = extract_metadata_from_node(value)
            if metadata:
                return metadata
        elif isinstance(value, list):
            for item in value:
                metadata = extract_metadata_from_node(item)
                if metadata:
                    return metadata

    return {}


def find_latest_xcresult_in_derived_data(derived_data_root=None):
    """Finds the most recently modified .xcresult bundle in Xcode DerivedData."""
    root = Path(derived_data_root or "~/Library/Developer/Xcode/DerivedData").expanduser()
    if not root.exists() or not root.is_dir():
        return None

    def latest_from_paths(paths):
        latest_path = None
        latest_mtime = -1.0
        for p in paths:
            if not p.is_dir() or p.suffix != ".xcresult":
                continue
            try:
                mtime = p.stat().st_mtime
            except Exception:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = p
        return latest_path

    try:
        # Most test results land under Logs/Test; check there first to avoid
        # scanning all DerivedData folders every run.
        fast_candidates = root.glob("*/Logs/Test/*.xcresult")
        latest_path = latest_from_paths(fast_candidates)

        # Fallback keeps behavior robust for atypical layouts.
        if latest_path is None:
            latest_path = latest_from_paths(root.rglob("*.xcresult"))
    except Exception:
        return None

    return str(latest_path) if latest_path else None


def resolve_manual_xcresult(xcresult_input):
    """Resolves a user-provided xcresult path or bundle name."""
    if not xcresult_input:
        return None

    raw = str(xcresult_input).strip()
    if not raw:
        return None

    direct = Path(raw).expanduser()
    if direct.exists() and direct.is_dir() and direct.suffix == ".xcresult":
        return str(direct)

    # Allow passing just a bundle name without extension.
    if not direct.name.endswith(".xcresult"):
        with_ext = direct.with_name(direct.name + ".xcresult")
        if with_ext.exists() and with_ext.is_dir() and with_ext.suffix == ".xcresult":
            return str(with_ext)

    # Allow passing only a folder name that exists in the current directory.
    local = Path.cwd() / raw
    if local.exists() and local.is_dir() and local.suffix == ".xcresult":
        return str(local)

    local_with_ext = Path.cwd() / (raw if raw.endswith(".xcresult") else f"{raw}.xcresult")
    if local_with_ext.exists() and local_with_ext.is_dir() and local_with_ext.suffix == ".xcresult":
        return str(local_with_ext)

    return None





def load_xcresult_test_results(bundle_path):
    if not os.path.exists(bundle_path):
        print(f"Error: {bundle_path} not found.")
        return None

    try:
        result = subprocess.run(
            [
                'xcrun',
                'xcresulttool',
                'get',
                'test-results',
                'tests',
                '--path',
                bundle_path,
                '--format',
                'json',
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            print(f"Error reading xcresult bundle: {result.stderr.strip()}")
            return None

        return json.loads(result.stdout)
    except Exception as e:
        print(f"Error invoking xcresulttool: {e}")
        return None


def load_test_summary(bundle_path):
    """Reads the xcresult test-results summary once and returns it as a dict.

    The summary feeds both the wall-clock duration and the Top Insights, so it
    is fetched a single time and shared to avoid invoking xcresulttool twice.
    """
    try:
        result = subprocess.run(
            [
                'xcrun',
                'xcresulttool',
                'get',
                'test-results',
                'summary',
                '--path',
                bundle_path,
                '--format',
                'json',
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0 or not result.stdout:
            return None

        return json.loads(result.stdout)
    except Exception:
        return None


def get_wall_clock_duration(summary):
    """Returns the wall-clock run duration (finishTime - startTime) in seconds.

    This matches Xcode's "Ran for ..." figure, which includes app launches,
    setup/teardown and gaps between tests, unlike the sum of individual test
    durations.
    """
    if not isinstance(summary, dict):
        return None
    start = summary.get('startTime')
    finish = summary.get('finishTime')
    if isinstance(start, (int, float)) and isinstance(finish, (int, float)) and finish >= start:
        return round(float(finish) - float(start), 2)
    return None


def get_top_insights(summary):
    """Returns the list of Top Insights from an xcresult summary dict.

    Mirrors the "Top Insights" section Xcode shows when opening an xcresult.
    Each insight is a dict with 'category', 'impact' and 'text'.
    """
    if not isinstance(summary, dict):
        return []
    insights = summary.get('topInsights')
    if isinstance(insights, list):
        return [i for i in insights if isinstance(i, dict)]
    return []


def load_coverage_data(bundle_path):
    """Runs xcrun xccov to extract coverage information as JSON."""
    try:
        result = subprocess.run(
            [
                'xcrun',
                'xccov',
                'view',
                '--report',
                '--json',
                bundle_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0 or not result.stdout:
            return None

        return json.loads(result.stdout)
    except Exception:
        return None


def get_simulator_info_from_db(db_path):
    """Extracts the run destination / device info from the xcresult sqlite DB."""
    if not db_path or not os.path.exists(db_path):
        return {}

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Try to get the first RunDestination and map to Devices
        cursor.execute("SELECT device_fk FROM RunDestinations LIMIT 1")
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {}

        device_fk = row[0]
        cursor.execute(
            "SELECT name, modelName, operatingSystemVersion, operatingSystemVersionWithBuildNumber FROM Devices WHERE rowid=?",
            (device_fk,)
        )
        d = cursor.fetchone()
        conn.close()
        if not d:
            return {}

        return {
            'name': d[0],
            'model': d[1],
            'os_version': d[2],
            'os_version_build': d[3],
        }
    except Exception:
        return {}


def parse_xcresulttool_results(data):
    metrics = {"passed": 0, "failed": 0, "skipped": 0, "total": 0, "duration": 0}
    categories = {}
    test_id_counter = {}

    def normalize_status(status):
        if not status:
            return "unknown"
        s = str(status).strip().lower()
        return {
            "success": "passed",
            "passed": "passed",
            "failure": "failed",
            "failed": "failed",
            "skipped": "skipped",
        }.get(s, s)

    def parse_duration(node):
        duration = node.get("durationInSeconds")
        if isinstance(duration, (int, float)):
            return float(duration)

        duration_str = node.get("duration")
        if isinstance(duration_str, str):
            try:
                seconds = 0.0
                if "m" in duration_str:
                    parts = duration_str.split()
                    for part in parts:
                        if part.endswith('m'):
                            seconds += float(part[:-1]) * 60
                        elif part.endswith('s'):
                            seconds += float(part[:-1])
                elif duration_str.endswith('s'):
                    seconds += float(duration_str[:-1])
                return seconds
            except Exception:
                return 0.0

        return 0.0

    def walk(node, current_suite=None):
        if not isinstance(node, dict):
            return

        node_type = node.get("nodeType", "")
        if node_type == "Test Case":
            suite_name = current_suite or node.get("name", "General Tests")
            if suite_name not in categories:
                categories[suite_name] = []
                test_id_counter[suite_name] = 1

            status = normalize_status(node.get("result", "unknown"))
            duration = parse_duration(node)
            default_srs_id = f"SRS-{suite_name.replace(' ', '')}-{test_id_counter[suite_name]}"
            metadata = extract_metadata_from_node(node)
            srs_id = parse_srs_id_from_metadata(metadata, default_srs_id)
            requirements = normalize_requirements(metadata.get("requirements"))
            description = metadata.get("description", "") if isinstance(metadata, dict) else ""
            cat_desc = metadata.get("category_description", "") if isinstance(metadata, dict) else ""
            test_id_counter[suite_name] += 1

            # Failure messages are nested child nodes of type "Failure Message";
            # their text lives in the child's "name" field.
            failure_messages = []
            for child in node.get("children", []) or []:
                if isinstance(child, dict) and child.get("nodeType") == "Failure Message":
                    msg = str(child.get("name", "")).strip()
                    if msg:
                        failure_messages.append(msg)
            error_text = node.get("failureSummary", "") or "\n".join(failure_messages)

            categories[suite_name].append(
                {
                    "srs_id": srs_id,
                    "name": node.get("name", "Unknown Test"),
                    "status": status,
                    "duration": round(duration, 3),
                    "error": error_text,
                    "description": description,
                    "requirements": requirements,
                    "category_description": cat_desc,
                }
            )

            if status == "passed":
                metrics["passed"] += 1
            elif status == "failed":
                metrics["failed"] += 1
            elif status == "skipped":
                metrics["skipped"] += 1

            metrics["duration"] += duration
            return

        next_suite = current_suite
        if node_type in ("Test Suite", "Unit test bundle", "UI test bundle", "Test Plan"):
            next_suite = node.get("name", current_suite)

        for child in node.get("children", []):
            walk(child, next_suite)

    for test_node in data.get("testNodes", []):
        walk(test_node, None)

    metrics["total"] = metrics["passed"] + metrics["failed"] + metrics["skipped"]
    metrics["duration"] = round(metrics["duration"], 2)
    return metrics, categories


def enrich_categories_with_database_metadata(categories, db_results, bundle_path=None):
    if not categories or not db_results:
        return categories

    for row in db_results:
        suite_name = row['suite_name'] or "General Tests"
        test_name = row['test_name'] or "Unknown Test"
        if suite_name not in categories:
            continue

        payload_ref = row['payload_ref'] if 'payload_ref' in row.keys() else None
        metadata = load_attachment_metadata(bundle_path, payload_ref) if payload_ref else {}
        if not isinstance(metadata, dict) or not metadata:
            continue

        srs_id = parse_srs_id_from_metadata(metadata, None)
        requirements = normalize_requirements(metadata.get('requirements'))
        description = metadata.get('description', "") if isinstance(metadata, dict) else ""
        cat_desc = metadata.get('category_description', "") if isinstance(metadata, dict) else ""

        for test in categories[suite_name]:
            if test.get('name') == test_name:
                if srs_id:
                    test['srs_id'] = srs_id
                if description:
                    test['description'] = description
                if requirements:
                    test['requirements'] = requirements
                if cat_desc:
                    test['category_description'] = cat_desc
                break

    return categories


def format_duration(seconds):
    """Format seconds into a human-readable 'Xm Ys' string."""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "0s"
    if total < 0:
        total = 0
    mins = int(total // 60)
    secs = total % 60
    if mins > 0:
        return f"{mins}m {secs:.1f}s"
    else:
        return f"{secs:.1f}s"


def collect_problem_entries(categories):
    """Builds a flat list of tests that failed, skipped, or logged an error."""
    entries = []
    for category_name, test_cases in (categories or {}).items():
        for test in test_cases:
            status = str(test.get('status', '')).lower()
            error = (test.get('error') or '').strip()
            if error or status in ('failed', 'skipped'):
                entries.append(
                    {
                        'category': category_name,
                        'name': test.get('name', 'Unknown Test'),
                        'status': status,
                        'duration': test.get('duration', 0),
                        'error': error,
                        'srs_id': test.get('srs_id', ''),
                    }
                )
    return entries


def flatten_coverage_files(cov):
    """Flattens coverage targets into one sortable file list."""
    files = []
    if not isinstance(cov, dict):
        return files
    for target in cov.get('targets', []) or []:
        for f in target.get('files', []) or []:
            executable = f.get('executableLines') or 0
            covered = f.get('coveredLines') or 0
            percent = round((covered / executable) * 100, 2) if executable else 0
            files.append(
                {
                    'path': f.get('path'),
                    'covered': covered,
                    'executable': executable,
                    'percent': percent,
                }
            )
    return files


def _coverage_bar_colors(percent):
    """Returns (bar_color, text_color) for a given coverage percentage."""
    if percent >= 75:
        return "#10B981", "text-green-600"
    if percent >= 40:
        return "#F59E0B", "text-amber-600"
    return "#EF4444", "text-red-600"


def generate_coverage_page(coverage, output_dir, overview_filename="report.html", filename="coverage.html"):
    """Generates a dedicated full code-coverage page listing every file,
    grouped by target, each with a horizontal progress bar (like Xcode)."""
    if not isinstance(coverage, dict):
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    overall_covered = coverage.get('coveredLines')
    overall_exec = coverage.get('executableLines')
    overall_line_cov = coverage.get('lineCoverage')
    overall_percent = round(float(overall_line_cov) * 100, 2) if overall_line_cov is not None else 0

    targets = coverage.get('targets', []) or []
    total_files = 0
    targets_html = ""

    for t in targets:
        target_name = t.get('name', 'Target')
        t_files = t.get('files', []) or []
        if not t_files:
            continue

        files = []
        for f in t_files:
            executable = f.get('executableLines') or 0
            covered = f.get('coveredLines') or 0
            percent = round((covered / executable) * 100, 2) if executable else 0
            files.append({
                'name': os.path.basename(f.get('path') or ''),
                'covered': covered,
                'executable': executable,
                'percent': percent,
            })

        # Order largest files first, mirroring the report's existing convention
        files = sorted(files, key=lambda x: (-x['executable'], -x['percent']))
        total_files += len(files)

        t_covered = t.get('coveredLines')
        if t_covered is None:
            t_covered = sum(f['covered'] for f in files)
        t_exec = t.get('executableLines')
        if t_exec is None:
            t_exec = sum(f['executable'] for f in files)
        t_line_cov = t.get('lineCoverage')
        t_percent = round(float(t_line_cov) * 100, 2) if t_line_cov is not None else (round((t_covered / t_exec) * 100, 2) if t_exec else 0)
        t_bar, t_text = _coverage_bar_colors(t_percent)

        rows_html = ""
        for f in files:
            bar_color, text_color = _coverage_bar_colors(f['percent'])
            rows_html += f"""
                <div class=\"coverage-file flex items-center gap-4 px-4 py-2.5 border-b border-slate-100 hover:bg-slate-50 transition-colors\" data-name=\"{f['name'].lower()}\" data-percent=\"{f['percent']}\">
                    <span class=\"flex-1 min-w-0 truncate text-sm text-slate-700 font-mono\" title=\"{f['name']}\">{f['name']}</span>
                    <div class=\"hidden sm:block w-48 lg:w-72 bg-slate-200 rounded-full h-2 overflow-hidden flex-shrink-0\">
                        <div class=\"h-2 rounded-full\" style=\"width:{f['percent']}%; background:{bar_color};\"></div>
                    </div>
                    <span class=\"w-16 text-right text-sm font-semibold {text_color} flex-shrink-0\">{f['percent']}%</span>
                    <span class=\"w-20 text-right text-xs text-slate-400 font-mono flex-shrink-0\">{f['executable']}</span>
                </div>"""

        targets_html += f"""
            <div class=\"coverage-target bg-white rounded-2xl shadow-sm border border-gray-200 overflow-hidden mb-6\" data-name=\"{target_name.lower()}\">
                <div class=\"flex items-center gap-4 px-4 py-3 bg-slate-50 border-b border-slate-200\">
                    <svg class=\"w-5 h-5 text-slate-400 flex-shrink-0\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\" stroke-width=\"1.8\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M3.75 9.75h16.5m-16.5 4.5h16.5M3.75 5.25h16.5M3.75 18.75h16.5\"/></svg>
                    <span class=\"flex-1 min-w-0 truncate font-bold text-slate-800\" title=\"{target_name}\">{target_name}</span>
                    <div class=\"hidden sm:block w-48 lg:w-72 bg-slate-200 rounded-full h-2 overflow-hidden flex-shrink-0\">
                        <div class=\"h-2 rounded-full\" style=\"width:{t_percent}%; background:{t_bar};\"></div>
                    </div>
                    <span class=\"w-16 text-right text-sm font-bold {t_text} flex-shrink-0\">{t_percent}%</span>
                    <span class=\"w-20 text-right text-xs text-slate-500 font-mono flex-shrink-0\">{t_exec}</span>
                </div>
                <div class=\"coverage-files divide-y divide-slate-50\">
                    {rows_html}
                </div>
            </div>"""

    overall_bar, overall_text = _coverage_bar_colors(overall_percent)

    page_html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>Code Coverage — Test Dashboard</title>
    <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
    <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
    <link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap\" rel=\"stylesheet\">
    <script src=\"https://cdn.tailwindcss.com\"></script>
    <style>
        * {{ font-family: 'Inter', system-ui, -apple-system, sans-serif; }}
    </style>
</head>
<body class=\"bg-slate-50 text-gray-900 antialiased min-h-screen\">
    <header class=\"text-white shadow-lg\" style=\"background: linear-gradient(135deg, #0f172a 0%, #111827 50%, #000000 100%);\">
        <div class=\"max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8\">
            <div class=\"flex justify-between items-center\">
                <div>
                    <p class=\"text-white/60 text-xs uppercase tracking-widest mb-1\">Report</p>
                    <h1 class=\"text-2xl sm:text-3xl font-extrabold tracking-tight\">Code Coverage</h1>
                    <p class=\"text-white/70 text-sm mt-1\">{total_files} files across {len([t for t in targets if (t.get('files') or [])])} targets</p>
                </div>
                <div class=\"text-right flex items-center gap-4\">
                    <div class=\"bg-white/15 backdrop-blur-sm rounded-xl px-5 py-3 border border-white/20\">
                        <span class=\"text-3xl font-mono font-bold\">{overall_percent}%</span>
                        <p class=\"text-[10px] text-white/70 uppercase tracking-widest mt-0.5\">Coverage</p>
                    </div>
                    <a href=\"../{overview_filename}\" class=\"hidden sm:flex items-center gap-2 px-4 py-2.5 rounded-xl bg-white/15 hover:bg-white/25 border border-white/20 text-white text-sm font-medium transition-all\">
                        <svg class=\"w-4 h-4\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\" stroke-width=\"2\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M10 19l-7-7m0 0l7-7m-7 7h18\"/></svg>
                        <span>Overview</span>
                    </a>
                </div>
            </div>
        </div>
    </header>

    <main class=\"max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8\">
        <div class=\"grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6\">
            <div class=\"bg-white p-4 rounded-xl border border-gray-200 shadow-sm\"><div class=\"text-sm text-slate-500\">Covered Lines</div><div class=\"text-2xl font-bold text-green-700\">{overall_covered}</div></div>
            <div class=\"bg-white p-4 rounded-xl border border-gray-200 shadow-sm\"><div class=\"text-sm text-slate-500\">Executable Lines</div><div class=\"text-2xl font-bold text-slate-700\">{overall_exec}</div></div>
            <div class=\"bg-white p-4 rounded-xl border border-gray-200 shadow-sm\"><div class=\"text-sm text-slate-500\">Overall Coverage</div><div class=\"text-2xl font-bold {overall_text}\">{overall_percent}%</div></div>
        </div>

        <div class=\"bg-white rounded-2xl shadow-sm border border-gray-200 p-4 mb-6\">
            <div class=\"grid gap-4 md:grid-cols-2 items-end\">
                <label class=\"block\">
                    <span class=\"text-xs font-semibold text-slate-500 uppercase tracking-wider\">Search files</span>
                    <input id=\"coverageSearch\" type=\"search\" class=\"mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all\" placeholder=\"Search file names...\" />
                </label>
                <label class=\"block\">
                    <span class=\"text-xs font-semibold text-slate-500 uppercase tracking-wider\">Filter coverage</span>
                    <select id=\"coverageFilter\" class=\"mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all\">
                        <option value=\"all\">All files</option>
                        <option value=\"high\">High (≥ 75%)</option>
                        <option value=\"medium\">Medium (40–75%)</option>
                        <option value=\"low\">Low (&lt; 40%)</option>
                        <option value=\"zero\">Uncovered (0%)</option>
                    </select>
                </label>
            </div>
            <div class=\"flex items-center justify-between mt-3 px-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400\">
                <span>File</span>
                <div class=\"flex items-center gap-4\">
                    <span class=\"hidden sm:inline w-48 lg:w-72 text-left\">Coverage</span>
                    <span class=\"w-16 text-right\">%</span>
                    <span class=\"w-20 text-right\">Lines</span>
                </div>
            </div>
        </div>

        <div id=\"coverageTargets\">
            {targets_html if targets_html else '<p class="text-slate-500">No coverage data available.</p>'}
        </div>
        <div id=\"coverageNoResults\" class=\"hidden text-center py-12 text-slate-400\">
            <p class=\"text-lg font-medium\">No matching files found</p>
            <p class=\"text-sm mt-1\">Try adjusting your search or filter criteria.</p>
        </div>
    </main>

    <footer class=\"mt-8 py-8 border-t border-gray-200\">
        <div class=\"max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center text-sm text-slate-500\">Automated Test Report</div>
    </footer>

    <script>
        function filterCoverage() {{
            const q = document.getElementById('coverageSearch').value.toLowerCase().trim();
            const mode = document.getElementById('coverageFilter').value;
            let totalVisible = 0;
            document.querySelectorAll('.coverage-target').forEach(target => {{
                let visibleInTarget = 0;
                target.querySelectorAll('.coverage-file').forEach(row => {{
                    const name = row.dataset.name || '';
                    const pct = parseFloat(row.dataset.percent || '0');
                    const matchesSearch = !q || name.includes(q);
                    let matchesFilter = true;
                    if (mode === 'high') matchesFilter = pct >= 75;
                    else if (mode === 'medium') matchesFilter = pct >= 40 && pct < 75;
                    else if (mode === 'low') matchesFilter = pct < 40;
                    else if (mode === 'zero') matchesFilter = pct === 0;
                    const show = matchesSearch && matchesFilter;
                    row.style.display = show ? '' : 'none';
                    if (show) visibleInTarget++;
                }});
                target.style.display = visibleInTarget > 0 ? '' : 'none';
                totalVisible += visibleInTarget;
            }});
            const noResults = document.getElementById('coverageNoResults');
            if (noResults) noResults.classList.toggle('hidden', totalVisible !== 0);
        }}
        document.getElementById('coverageSearch').addEventListener('input', filterCoverage);
        document.getElementById('coverageFilter').addEventListener('change', filterCoverage);
    </script>
</body>
</html>
"""

    coverage_path = output_dir / filename
    try:
        coverage_path.write_text(page_html, encoding='utf-8')
    except Exception:
        return None
    return filename


def _html_escape(text):
    """Minimal HTML escaping for log/error text."""
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def generate_logs_page(categories, output_dir, overview_filename="report.html", filename="logs.html"):
    """Generates a dedicated logs page listing every test that produced an error,
    along with its key details (category, SRS ID, status, duration, error text)."""
    if not categories:
        return None, 0

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log_entries = collect_problem_entries(categories)

    if not log_entries:
        return None, 0

    # Failed entries first, then skipped, then the rest; alphabetical within
    status_order = {'failed': 0, 'skipped': 1}
    log_entries.sort(key=lambda e: (status_order.get(e['status'], 2), e['category'].lower(), e['name'].lower()))

    failed_count = sum(1 for e in log_entries if e['status'] == 'failed')
    skipped_count = sum(1 for e in log_entries if e['status'] == 'skipped')

    rows_html = ""
    for e in log_entries:
        status = e['status']
        if status == 'failed':
            badge = 'text-red-700 bg-red-100'
            accent = 'border-red-500'
            icon = '✗'
        elif status == 'skipped':
            badge = 'text-yellow-700 bg-yellow-100'
            accent = 'border-yellow-500'
            icon = '⊘'
        else:
            badge = 'text-slate-700 bg-slate-100'
            accent = 'border-slate-400'
            icon = 'ℹ'

        error_html = ""
        if e['error']:
            error_html = f"""
                    <div class=\"mt-3 p-3 bg-slate-900 text-slate-100 rounded-lg text-xs font-mono whitespace-pre-wrap break-words overflow-x-auto\">{_html_escape(e['error'])}</div>"""

        searchable = f"{e['name']} {e['category']} {e['srs_id']} {e['error']}".lower().replace('"', '')
        rows_html += f"""
                <div class=\"log-entry border-l-4 {accent} bg-white rounded-r-xl shadow-sm border border-gray-200 p-4\" data-status=\"{status}\" data-category=\"{e['category'].lower()}\" data-search=\"{searchable}\">
                    <div class=\"flex flex-wrap items-start justify-between gap-3\">
                        <div class=\"min-w-0\">
                            <div class=\"flex items-center gap-2\">
                                <span class=\"text-lg\">{icon}</span>
                                <span class=\"font-semibold text-gray-900 break-words\">{e['name']}</span>
                            </div>
                            <p class=\"text-xs text-slate-500 mt-1\">{e['category']}</p>
                        </div>
                        <div class=\"flex items-center gap-2 flex-shrink-0\">
                            <span class=\"inline-block px-3 py-1 rounded-full text-xs font-semibold {badge}\">{status.upper()}</span>
                            <span class=\"text-xs text-slate-400 font-mono\">{format_duration(e['duration'])}</span>
                        </div>
                    </div>
                    <div class=\"mt-2 text-xs text-slate-600\"><span class=\"font-semibold text-slate-500\">SRS ID:</span> <code class=\"bg-gray-100 px-2 py-0.5 rounded\">{_html_escape(e['srs_id'])}</code></div>
                    {error_html}
                </div>"""

    # Category filter options
    cat_options = "".join(
        f"<option value=\"{c.lower()}\">{c}</option>"
        for c in sorted({e['category'] for e in log_entries}, key=lambda x: x.lower())
    )

    page_html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>Test Logs — Test Dashboard</title>
    <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
    <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
    <link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap\" rel=\"stylesheet\">
    <script src=\"https://cdn.tailwindcss.com\"></script>
    <style>
        * {{ font-family: 'Inter', system-ui, -apple-system, sans-serif; }}
    </style>
</head>
<body class=\"bg-slate-50 text-gray-900 antialiased min-h-screen\">
    <header class=\"text-white shadow-lg\" style=\"background: linear-gradient(135deg, #0f172a 0%, #111827 50%, #000000 100%);\">
        <div class=\"max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8\">
            <div class=\"flex justify-between items-center\">
                <div>
                    <p class=\"text-white/60 text-xs uppercase tracking-widest mb-1\">Report</p>
                    <h1 class=\"text-2xl sm:text-3xl font-extrabold tracking-tight\">Test Logs</h1>
                    <p class=\"text-white/70 text-sm mt-1\">{len(log_entries)} entries · {failed_count} failed · {skipped_count} skipped</p>
                </div>
                <div class=\"text-right flex items-center gap-4\">
                    <div class=\"bg-white/15 backdrop-blur-sm rounded-xl px-5 py-3 border border-white/20\">
                        <span class=\"text-3xl font-mono font-bold\">{len(log_entries)}</span>
                        <p class=\"text-[10px] text-white/70 uppercase tracking-widest mt-0.5\">Entries</p>
                    </div>
                    <a href=\"../{overview_filename}\" class=\"hidden sm:flex items-center gap-2 px-4 py-2.5 rounded-xl bg-white/15 hover:bg-white/25 border border-white/20 text-white text-sm font-medium transition-all\">
                        <svg class=\"w-4 h-4\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\" stroke-width=\"2\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M10 19l-7-7m0 0l7-7m-7 7h18\"/></svg>
                        <span>Overview</span>
                    </a>
                </div>
            </div>
        </div>
    </header>

    <main class=\"max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8\">
        <div class=\"grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6\">
            <div class=\"bg-white p-4 rounded-xl border border-gray-200 shadow-sm\"><div class=\"text-sm text-slate-500\">Total Entries</div><div class=\"text-2xl font-bold text-slate-700\">{len(log_entries)}</div></div>
            <div class=\"bg-white p-4 rounded-xl border border-gray-200 shadow-sm\"><div class=\"text-sm text-slate-500\">Failed</div><div class=\"text-2xl font-bold text-red-600\">{failed_count}</div></div>
            <div class=\"bg-white p-4 rounded-xl border border-gray-200 shadow-sm\"><div class=\"text-sm text-slate-500\">Skipped</div><div class=\"text-2xl font-bold text-yellow-600\">{skipped_count}</div></div>
        </div>

        <div class=\"bg-white rounded-2xl shadow-sm border border-gray-200 p-4 mb-6\">
            <div class=\"grid gap-4 md:grid-cols-3 items-end\">
                <label class=\"block\">
                    <span class=\"text-xs font-semibold text-slate-500 uppercase tracking-wider\">Search logs</span>
                    <input id=\"logSearch\" type=\"search\" class=\"mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all\" placeholder=\"Search test name, error, SRS ID...\" />
                </label>
                <label class=\"block\">
                    <span class=\"text-xs font-semibold text-slate-500 uppercase tracking-wider\">Filter status</span>
                    <select id=\"logStatusFilter\" class=\"mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all\">
                        <option value=\"all\">All</option>
                        <option value=\"failed\">Failed only</option>
                        <option value=\"skipped\">Skipped only</option>
                    </select>
                </label>
                <label class=\"block\">
                    <span class=\"text-xs font-semibold text-slate-500 uppercase tracking-wider\">Filter category</span>
                    <select id=\"logCategoryFilter\" class=\"mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all\">
                        <option value=\"all\">All categories</option>
                        {cat_options}
                    </select>
                </label>
            </div>
        </div>

        <div id=\"logEntries\" class=\"space-y-3\">
            {rows_html}
        </div>
        <div id=\"logNoResults\" class=\"hidden text-center py-12 text-slate-400\">
            <p class=\"text-lg font-medium\">No matching log entries</p>
            <p class=\"text-sm mt-1\">Try adjusting your search or filter criteria.</p>
        </div>
    </main>

    <footer class=\"mt-8 py-8 border-t border-gray-200\">
        <div class=\"max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center text-sm text-slate-500\">Automated Test Report</div>
    </footer>

    <script>
        function filterLogs() {{
            const q = document.getElementById('logSearch').value.toLowerCase().trim();
            const statusMode = document.getElementById('logStatusFilter').value;
            const catMode = document.getElementById('logCategoryFilter').value;
            let visible = 0;
            document.querySelectorAll('.log-entry').forEach(entry => {{
                const search = entry.dataset.search || '';
                const status = entry.dataset.status || '';
                const category = entry.dataset.category || '';
                const matchesSearch = !q || search.includes(q);
                const matchesStatus = statusMode === 'all' || status === statusMode;
                const matchesCat = catMode === 'all' || category === catMode;
                const show = matchesSearch && matchesStatus && matchesCat;
                entry.style.display = show ? '' : 'none';
                if (show) visible++;
            }});
            const noResults = document.getElementById('logNoResults');
            if (noResults) noResults.classList.toggle('hidden', visible !== 0);
        }}
        document.getElementById('logSearch').addEventListener('input', filterLogs);
        document.getElementById('logStatusFilter').addEventListener('change', filterLogs);
        document.getElementById('logCategoryFilter').addEventListener('change', filterLogs);
    </script>
</body>
</html>
"""

    logs_path = output_dir / filename
    try:
        logs_path.write_text(page_html, encoding='utf-8')
    except Exception:
        return None, 0
    return filename, len(log_entries)


def generate_html_report(metrics, categories, output_filename="report.html", coverage=None, device_info=None, output_dir=None):
    """Generates a beautifully styled, customizable HTML report with charts."""

    # Prepare output directories
    output_dir = Path(output_dir) if output_dir else Path("reports")
    categories_dir = output_dir / "categories"
    output_dir.mkdir(parents=True, exist_ok=True)
    categories_dir.mkdir(parents=True, exist_ok=True)

    # Serialise data for Chart.js injection
    chart_labels = ["Passed", "Failed", "Skipped"]
    chart_data = [metrics["passed"], metrics["failed"], metrics["skipped"]]

    # Prepare device and coverage placeholders (populated when calling with coverage/device_info)
    header_device_html = ""
    extra_overview_html = ""
    # coverage_summary and files list
    coverage_summary = None
    coverage_files_html = ""
    # Coverage and device info come straight from the function parameters.
    cov = coverage
    dev = device_info
    all_coverage_files = flatten_coverage_files(cov) if isinstance(cov, dict) else []

    if isinstance(dev, dict) and dev:
        header_device_html = f"<p class=\"text-sm text-slate-200 mt-1 text-white\">Simulator: {dev.get('name')} ({dev.get('model')}) — iOS {dev.get('os_version')} {dev.get('os_version_build') or ''}</p>"

    # Build the "Top Insights" component (mirrors Xcode's Top Insights section)
    insights_overview_html = ""
    insights = metrics.get('top_insights') if isinstance(metrics, dict) else None
    if isinstance(insights, list) and insights:
        insight_cards_html = ""
        for ins in insights:
            category = str(ins.get('category', 'Insight'))
            impact = str(ins.get('impact', ''))
            text = str(ins.get('text', ''))
            cat_lower = category.lower()
            if 'fail' in cat_lower:
                accent = 'border-red-400 bg-red-50'
                badge = 'text-red-700 bg-red-100'
                icon = '⚠'
            elif 'performance' in cat_lower or 'slow' in cat_lower:
                accent = 'border-amber-400 bg-amber-50'
                badge = 'text-amber-700 bg-amber-100'
                icon = '⏱'
            else:
                accent = 'border-blue-400 bg-blue-50'
                badge = 'text-blue-700 bg-blue-100'
                icon = 'ℹ'
            impact_badge = f"<span class=\"inline-flex items-center rounded-full px-3 py-1 text-xs font-semibold {badge}\">{impact}</span>" if impact else ""
            insight_cards_html += f"""
                <div class=\"border-l-4 {accent} rounded-r-lg p-4\">
                    <div class=\"flex items-start justify-between gap-3 mb-1\">
                        <div class=\"flex items-center gap-2\">
                            <span class=\"text-lg\">{icon}</span>
                            <span class=\"font-semibold text-gray-800\">{category}</span>
                        </div>
                        {impact_badge}
                    </div>
                    <p class=\"text-sm text-slate-700 font-mono whitespace-pre-wrap break-words\">{text}</p>
                </div>"""

        insights_overview_html = f"""<details open class=\"section-card bg-white rounded-2xl shadow-sm border border-gray-200 p-6 group\">
                    <summary class=\"flex items-center justify-between cursor-pointer list-none\">
                        <div class=\"flex items-center gap-2\">
                            <h2 class=\"text-xl font-bold text-gray-800\">Top Insights</h2>
                            <span class=\"inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-semibold text-slate-600\">{len(insights)}</span>
                        </div>
                        <span class=\"text-xl text-slate-500 transition-transform duration-200 group-open:rotate-180\">▾</span>
                    </summary>
                    <div class=\"mt-4 space-y-3\">
                        {insight_cards_html}
                    </div>
                </details>"""

    if isinstance(cov, dict):
        total_cov_lines = cov.get('coveredLines')
        total_exec_lines = cov.get('executableLines')
        line_cov = cov.get('lineCoverage')
        coverage_summary = {
            'covered': total_cov_lines,
            'executable': total_exec_lines,
            'percent': round(float(line_cov) * 100, 2) if line_cov is not None else None,
        }

        files = sorted(all_coverage_files, key=lambda x: (-x.get('executable', 0), -x.get('percent', 0)))
        top_files = files[:20]
        if top_files:
            coverage_files_html = """
            <div class=\"bg-white p-4 rounded border border-gray-200\">\n                <h3 class=\"text-lg font-semibold mb-2\">Coverage - Top Files</h3>\n                <table class=\"w-full text-sm\">"""
            coverage_files_html += "<tr class=\"text-left text-xs text-slate-500\"><th>File</th><th>Covered</th><th>Executable</th><th>%</th></tr>"
            for f in top_files:
                name = os.path.basename(f.get('path') or '')
                coverage_files_html += f"<tr><td class=\"py-1\">{name}</td><td>{f.get('covered')}</td><td>{f.get('executable')}</td><td>{f.get('percent')}%</td></tr>"
            coverage_files_html += "</table></div>"

        if coverage_summary:
            coverage_page_name = generate_coverage_page(cov, categories_dir, output_filename)
            coverage_page_link = ""
            if coverage_page_name:
                coverage_page_link = f"<a href=\"categories/{coverage_page_name}\" class=\"inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold transition-colors\"><span>View all files</span><svg class=\"w-4 h-4\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\" stroke-width=\"2\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M14 5l7 7m0 0l-7 7m7-7H3\"/></svg></a>"
            extra_overview_html = f"<details open class=\"bg-white p-6 rounded-lg shadow-sm border border-gray-200 group\">\n                <summary class=\"flex items-center justify-between cursor-pointer list-none\"><h2 class=\"text-xl font-bold text-gray-700\">Code Coverage</h2><span class=\"text-xl text-slate-500 transition-transform duration-200 group-open:rotate-180\">▾</span></summary>\n                <div class=\"grid grid-cols-3 gap-4 mt-4\">\n                    <div class=\"p-4 rounded border bg-green-50\"><div class=\"text-sm text-slate-500\">Covered Lines</div><div class=\"text-2xl font-bold text-green-700\">{coverage_summary['covered']}</div></div>\n                    <div class=\"p-4 rounded border bg-slate-50\"><div class=\"text-sm text-slate-500\">Executable Lines</div><div class=\"text-2xl font-bold text-slate-700\">{coverage_summary['executable']}</div></div>\n                    <div class=\"p-4 rounded border bg-blue-50\"><div class=\"text-sm text-slate-500\">Coverage</div><div class=\"text-2xl font-bold text-blue-700\">{coverage_summary['percent']}%</div></div>\n                </div>\n                <div class=\"flex items-center justify-between mt-4 mb-2\"><h3 class=\"text-lg font-semibold text-gray-700\">Top Files</h3>{coverage_page_link}</div>\n                <div>{coverage_files_html}</div>\n            </details>"

    # Build the "Logs" section (aggregates failing/skipped tests) + dedicated page
    logs_overview_html = ""
    logs_page_name, logs_count = generate_logs_page(categories, categories_dir, output_filename)
    if logs_page_name and logs_count:
        # Reuse the same aggregation logic as the dedicated logs page so both
        # sections always show the same set of problematic tests.
        preview_entries = collect_problem_entries(categories)
        status_order = {'failed': 0, 'skipped': 1}
        preview_entries.sort(key=lambda e: (status_order.get(e['status'], 2), e['category'].lower(), e['name'].lower()))

        failed_total = sum(1 for e in preview_entries if e['status'] == 'failed')
        skipped_total = sum(1 for e in preview_entries if e['status'] == 'skipped')

        preview_rows = ""
        for e in preview_entries[:5]:
            if e['status'] == 'failed':
                badge = 'text-red-700 bg-red-100'
                accent = 'border-red-500'
                icon = '✗'
            elif e['status'] == 'skipped':
                badge = 'text-yellow-700 bg-yellow-100'
                accent = 'border-yellow-500'
                icon = '⊘'
            else:
                badge = 'text-slate-700 bg-slate-100'
                accent = 'border-slate-400'
                icon = 'ℹ'
            err_preview = ""
            if e['error']:
                snippet = e['error'].replace('\n', ' ')
                if len(snippet) > 160:
                    snippet = snippet[:160] + '…'
                err_preview = f"<p class=\"mt-1.5 text-xs text-slate-600 font-mono break-words\">{_html_escape(snippet)}</p>"
            preview_rows += f"""
                        <div class=\"border-l-4 {accent} bg-slate-50 rounded-r-lg px-3 py-2\">
                            <div class=\"flex items-center justify-between gap-2\">
                                <div class=\"min-w-0\">
                                    <span class=\"text-sm font-medium text-gray-900 break-words\">{icon} {e['name']}</span>
                                    <span class=\"block text-[11px] text-slate-400\">{e['category']}</span>
                                </div>
                                <span class=\"inline-block px-2.5 py-0.5 rounded-full text-[11px] font-semibold {badge} flex-shrink-0\">{e['status'].upper()}</span>
                            </div>
                            {err_preview}
                        </div>"""

        logs_page_link = f"<a href=\"categories/{logs_page_name}\" class=\"inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold transition-colors\"><span>View all logs</span><svg class=\"w-4 h-4\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\" stroke-width=\"2\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M14 5l7 7m0 0l-7 7m7-7H3\"/></svg></a>"

        logs_overview_html = f"""<details open class=\"section-card bg-white rounded-2xl shadow-sm border border-gray-200 p-6 group\">
                    <summary class=\"flex items-center justify-between cursor-pointer list-none\">
                        <div class=\"flex items-center gap-2\">
                            <h2 class=\"text-xl font-bold text-gray-800\">Logs</h2>
                            <span class=\"inline-flex items-center rounded-full bg-red-100 px-2.5 py-0.5 text-xs font-semibold text-red-700\">{failed_total} failed</span>
                            <span class=\"inline-flex items-center rounded-full bg-yellow-100 px-2.5 py-0.5 text-xs font-semibold text-yellow-700\">{skipped_total} skipped</span>
                        </div>
                        <span class=\"text-xl text-slate-500 transition-transform duration-200 group-open:rotate-180\">▾</span>
                    </summary>
                    <div class=\"mt-4 space-y-2\">
                        {preview_rows}
                    </div>
                    <div class=\"flex items-center justify-between mt-4 pt-4 border-t border-gray-100\">
                        <span class=\"text-sm text-slate-500\">Showing {min(5, logs_count)} of {logs_count} entries</span>
                        {logs_page_link}
                    </div>
                </details>"""

    # Build the test category dropdown items dynamically and create category pages
    accordion_html = ""
    for category_name, test_cases in categories.items():
        pass_in_cat = sum(1 for t in test_cases if t["status"] == "passed")
        fail_in_cat = sum(1 for t in test_cases if t["status"] == "failed")
        skip_in_cat = sum(1 for t in test_cases if t["status"] == "skipped")
        if pass_in_cat > 0 and fail_in_cat == 0 and skip_in_cat == 0:
            category_status = "passed-only"
        elif fail_in_cat > 0 and pass_in_cat == 0 and skip_in_cat == 0:
            category_status = "failed-only"
        else:
            category_status = "mixed"
        
        # Extract category description from test metadata (first non-empty wins)
        category_description = ""
        for t in test_cases:
            cd = t.get("category_description", "").strip()
            if cd:
                category_description = cd
                break
        if not category_description:
            category_description = f"This category contains {len(test_cases)} test cases. {pass_in_cat} passed, {fail_in_cat} failed, {skip_in_cat} skipped."

        # Link to per-category page
        slug = re.sub(r'[^a-z0-9]+', '-', category_name.lower()).strip('-')
        category_filename = f"category-{slug}.html"
        category_path = categories_dir / category_filename

        accordion_html += f"""
        <div id="section-cat-{slug}" class="category-section">
        <details class="category-card mb-4 bg-white border border-gray-200 rounded-lg shadow-sm overflow-hidden group" data-category-name="{category_name.lower()}" data-category-status="{category_status}">
            <summary class="cursor-pointer px-5 py-4 bg-gray-50 border-b border-gray-200 flex justify-between items-center text-left">
                <div>
                    <h3 class="text-lg font-bold text-gray-800"><a href="categories/{category_filename}" class="hover:underline">{category_name}</a></h3>
                    <p class="text-sm text-gray-500">{pass_in_cat} Passed / {fail_in_cat} Failed / {skip_in_cat} Skipped</p>
                </div>
                <span class="text-xl text-slate-500 transition-transform duration-200 group-open:rotate-180">▾</span>
            </summary>
            <div class="px-4 py-3 bg-blue-50 border-b border-blue-200 text-sm text-blue-800">
                <strong>Category Description:</strong> {category_description}
            </div>
            <div class="p-4 space-y-2">
        """

        for test in test_cases:
            status_lower = test["status"].lower()
            
            # Determine colors based on status
            if status_lower == "passed":
                status_color = "bg-green-50"
                border_color = "border-green-500"
                status_icon = "✓"
                status_badge = "text-green-700 bg-green-100"
            elif status_lower == "failed":
                status_color = "bg-red-50"
                border_color = "border-red-500"
                status_icon = "✗"
                status_badge = "text-red-700 bg-red-100"
            else:  # skipped
                status_color = "bg-yellow-50"
                border_color = "border-yellow-500"
                status_icon = "⊘"
                status_badge = "text-yellow-700 bg-yellow-100"

            metadata_snippet = ""
            if test.get("description") or test.get("requirements"):
                metadata_html_parts = []
                if test.get("description"):
                    metadata_html_parts.append(
                        f"<p class=\"mb-1\"><span class=\"font-semibold\">Description:</span> {test['description']}</p>"
                    )
                if test.get("requirements"):
                    srs_val = str(test.get('srs_id', '')).strip().lower()
                    filtered_requirements = [r for r in test["requirements"] if r and str(r).strip().lower() != srs_val]
                    if filtered_requirements:
                        req_badges = "".join(
                            f"<span class=\"inline-flex items-center rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700\">{req}</span>"
                            for req in filtered_requirements
                        )
                        metadata_html_parts.append(
                            f"<div class=\"flex flex-wrap gap-2\">{req_badges}</div>"
                        )
                metadata_snippet = f"<div class=\"mb-3 text-sm text-slate-700\">{''.join(metadata_html_parts)}</div>"

            error_snippet = ""
            if test.get("error", ""):
                error_snippet = f"""
                <div class=\"mt-2 p-3 bg-red-50 border border-red-200 rounded text-sm text-red-700 font-mono whitespace-pre-wrap\">
                    <strong>Error Log:</strong> {test['error']}
                </div>
                """

            accordion_html += f"""
            <div class="py-4 border-l-4 {border_color} pl-4 my-2 {status_color}">
                <div class="flex justify-between items-start mb-2 mr-2">
                    <div>
                        <span class="inline-block font-bold mr-2 text-lg">{status_icon}</span>
                        <span class="font-medium text-gray-900">{test['name']}</span>
                    </div>
                    <span class="text-xs text-gray-500 font-mono">{format_duration(test['duration'])}</span>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-2 text-sm mb-2">
                    <div class="text-gray-700">
                        <span class="font-semibold text-gray-600">SRS ID:</span> <code class="bg-gray-100 px-2 py-1 rounded">{test['srs_id']}</code>
                    </div>
                    <div class="text-gray-700">
                        <span class="font-semibold text-gray-600">Test Function:</span> {test['name']}
                    </div>
                    <div>
                        <span class="inline-block px-3 py-1 rounded-full text-xs font-semibold {status_badge}">
                            {status_lower.upper()}
                        </span>
                    </div>
                </div>
                {metadata_snippet}
                {error_snippet}
            </div>
            """

        accordion_html += "</div></details></div>"

        # Compute per-category metrics
        cat_pass = pass_in_cat
        cat_fail = fail_in_cat
        cat_skip = skip_in_cat
        cat_total = len(test_cases)
        cat_duration = round(sum(float(t.get('duration', 0) or 0) for t in test_cases), 2)



        category_test_entries_html = ""
        for test in test_cases:
            test_status = test['status'].lower()
            if test_status == 'passed':
                status_color = 'bg-green-50'
                border_color = 'border-green-500'
                status_icon = '✓'
                status_badge = 'text-green-700 bg-green-100'
            elif test_status == 'failed':
                status_color = 'bg-red-50'
                border_color = 'border-red-500'
                status_icon = '✗'
                status_badge = 'text-red-700 bg-red-100'
            else:
                status_color = 'bg-yellow-50'
                border_color = 'border-yellow-500'
                status_icon = '⊘'
                status_badge = 'text-yellow-700 bg-yellow-100'

            category_test_entries_html += f"""
                <div class=\"test-entry py-4 border-l-4 {border_color} pl-4 my-2 {status_color}\" data-status=\"{test_status}\" data-name=\"{test['name'].lower()}\">
                    <div class=\"flex justify-between items-start mb-2 mr-2\">
                        <div>
                            <span class=\"inline-block font-bold mr-2 text-lg\">{status_icon}</span>
                            <span class=\"font-medium text-gray-900\">{test['name']}</span>
                        </div>
                        <span class=\"text-xs text-gray-500 font-mono\">{format_duration(test['duration'])}</span>
                    </div>
                    <div class=\"grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-2 text-sm mb-2\">
                        <div class=\"text-gray-700\"><span class=\"font-semibold text-gray-600\">SRS ID:</span> <code class=\"bg-gray-100 px-2 py-1 rounded\">{test['srs_id']}</code></div>
                        <div class=\"text-gray-700\"><span class=\"font-semibold text-gray-600\">Test Function:</span> {test['name']}</div>
                        <div><span class=\"inline-block px-3 py-1 rounded-full text-xs font-semibold {status_badge}\">{test_status.upper()}</span></div>
                    </div>
                    <div class=\"text-sm text-slate-700\">{test.get('description','')}</div>
                </div>
            """

        # Create the per-category page
        chart_id = f"metricsChart-{slug}"
        
        # Build coverage section for this category - show only files related to this category
        category_coverage_html = ""
        if isinstance(cov, dict):
            # Extract test file names from the category tests to identify related source files
            test_files = set()
            for test in test_cases:
                test_name = test.get('name', '')
                # Try to extract file information from test name or use category name as hint
                if test_name:
                    # Remove common test prefixes/suffixes
                    clean_name = test_name.replace('test', '').replace('()', '').lower()
                    test_files.add(clean_name)
            
            # Extract keyword from category name to filter files
            category_keyword = re.sub(r'[^a-z0-9]', '', category_name.lower())
            
            # Reuse pre-flattened coverage files to avoid rebuilding the same list
            # for every category page.
            all_files = all_coverage_files

            # Filter files relevant to this category
            category_files = []
            for f in all_files:
                file_path = f.get('path', '').lower()
                file_name = os.path.basename(file_path).lower()
                
                # Match files that contain the category keyword or test file indicators
                if category_keyword in file_name or any(tf in file_name for tf in test_files if tf):
                    category_files.append(f)
            
            # If no files match directly, keep a soft fallback so the section
            # still provides useful context instead of showing blank data.
            if not category_files:
                for f in all_files:
                    file_path = f.get('path', '').lower()
                    file_name = os.path.basename(file_path).lower()
                    # Include if it's a test file or contains partial match
                    if 'test' in file_name or any(keyword in file_name for keyword in [category_keyword[:3], category_name.lower()[:3]]):
                        category_files.append(f)
            
            category_files = sorted(category_files, key=lambda x: (-x.get('executable', 0), -x.get('percent', 0)))
            
            # Calculate category-specific coverage metrics
            cat_covered_lines = sum(f.get('covered', 0) for f in category_files)
            cat_exec_lines = sum(f.get('executable', 0) for f in category_files)
            cat_coverage_percent = round((cat_covered_lines / cat_exec_lines) * 100, 2) if cat_exec_lines > 0 else 0
            
            coverage_files_html = ""
            if category_files:
                coverage_files_html = "<div class=\"bg-white p-4 rounded border border-gray-200 mt-4\"><h3 class=\"text-lg font-semibold mb-2\">Coverage - Category Files</h3><table class=\"w-full text-sm\"><tr class=\"text-left text-xs text-slate-500 border-b\"><th class=\"py-2\">File</th><th>Covered</th><th>Executable</th><th>%</th></tr>"
                for f in category_files:
                    name = os.path.basename(f.get('path') or '')
                    coverage_files_html += f"<tr class=\"border-b\"><td class=\"py-2\">{name}</td><td>{f.get('covered')}</td><td>{f.get('executable')}</td><td>{f.get('percent')}%</td></tr>"
                coverage_files_html += "</table></div>"
            
            if cat_exec_lines > 0:
                category_coverage_html = f"<details open class=\"bg-white p-6 rounded-lg shadow-sm border border-gray-200 group\"><summary class=\"flex items-center justify-between cursor-pointer list-none\"><h2 class=\"text-xl font-bold text-gray-700\">Code Coverage (Category)</h2><span class=\"text-xl text-slate-500 transition-transform duration-200 group-open:rotate-180\">▾</span></summary><div class=\"grid grid-cols-3 gap-4 mt-4\"><div class=\"p-4 rounded border bg-green-50\"><div class=\"text-sm text-slate-500\">Covered Lines</div><div class=\"text-2xl font-bold text-green-700\">{cat_covered_lines}</div></div><div class=\"p-4 rounded border bg-slate-50\"><div class=\"text-sm text-slate-500\">Executable Lines</div><div class=\"text-2xl font-bold text-slate-700\">{cat_exec_lines}</div></div><div class=\"p-4 rounded border bg-blue-50\"><div class=\"text-sm text-slate-500\">Coverage</div><div class=\"text-2xl font-bold text-blue-700\">{cat_coverage_percent}%</div></div></div>{coverage_files_html}</details>"
        
        # Build category sidebar items for category page
        cat_sidebar_items_html = ""
        for cn, tc in categories.items():
            s = re.sub(r'[^a-z0-9]+', '-', cn.lower()).strip('-')
            cfn = f"category-{s}.html"
            pc = sum(1 for t in tc if t['status'] == 'passed')
            fc = sum(1 for t in tc if t['status'] == 'failed')
            sc = sum(1 for t in tc if t['status'] == 'skipped')
            tc_len = len(tc)
            if fc > 0:
                dc = '#EF4444'
            elif sc > 0:
                dc = '#F59E0B'
            else:
                dc = '#10B981'
            is_active = ' active' if cn == category_name else ''
            active_style = ' font-weight:700; color:#1e293b;' if cn == category_name else ''
            cat_sidebar_items_html += f"""
                <a href=\"{cfn}\" class=\"sidebar-link group flex items-center gap-3 px-4 py-3 rounded-lg text-sm transition-all duration-200 hover:bg-slate-50{is_active}\">
                    <span class=\"flex-shrink-0 w-2.5 h-2.5 rounded-full\" style=\"background:{dc};\"></span>
                    <span class=\"flex-1 truncate sidebar-link-text\" style=\"{active_style}\">{cn}</span>
                    <span class=\"flex-shrink-0 text-xs opacity-60 font-mono\">{pc}/{tc_len}</span>
                </a>"""

        cat_pass_rate = round((cat_pass / cat_total) * 100, 1) if cat_total > 0 else 0

        # Build the per-category Logs section (failing / skipped tests in this category)
        category_logs_html = ""
        cat_log_entries = []
        for test in test_cases:
            st = str(test.get('status', '')).lower()
            err = (test.get('error') or '').strip()
            if err or st in ('failed', 'skipped'):
                cat_log_entries.append({
                    'name': test.get('name', 'Unknown Test'),
                    'status': st,
                    'duration': test.get('duration', 0),
                    'srs_id': test.get('srs_id', ''),
                    'error': err,
                })
        if cat_log_entries:
            log_status_order = {'failed': 0, 'skipped': 1}
            cat_log_entries.sort(key=lambda e: (log_status_order.get(e['status'], 2), e['name'].lower()))
            cat_logs_failed = sum(1 for e in cat_log_entries if e['status'] == 'failed')
            cat_logs_skipped = sum(1 for e in cat_log_entries if e['status'] == 'skipped')

            cat_log_rows = ""
            for e in cat_log_entries:
                if e['status'] == 'failed':
                    l_badge = 'text-red-700 bg-red-100'
                    l_accent = 'border-red-500'
                    l_icon = '✗'
                elif e['status'] == 'skipped':
                    l_badge = 'text-yellow-700 bg-yellow-100'
                    l_accent = 'border-yellow-500'
                    l_icon = '⊘'
                else:
                    l_badge = 'text-slate-700 bg-slate-100'
                    l_accent = 'border-slate-400'
                    l_icon = 'ℹ'

                l_error_html = ""
                if e['error']:
                    l_error_html = f"""
                            <div class=\"mt-3 p-3 bg-slate-900 text-slate-100 rounded-lg text-xs font-mono whitespace-pre-wrap break-words overflow-x-auto\">{_html_escape(e['error'])}</div>"""

                cat_log_rows += f"""
                        <div class=\"border-l-4 {l_accent} bg-white rounded-r-xl shadow-sm border border-gray-200 p-4\">
                            <div class=\"flex flex-wrap items-start justify-between gap-3\">
                                <div class=\"min-w-0\">
                                    <div class=\"flex items-center gap-2\">
                                        <span class=\"text-lg\">{l_icon}</span>
                                        <span class=\"font-semibold text-gray-900 break-words\">{e['name']}</span>
                                    </div>
                                </div>
                                <div class=\"flex items-center gap-3 flex-shrink-0\">
                                    <span class=\"inline-block px-3 py-1 rounded-full text-xs font-semibold {l_badge}\">{e['status'].upper()}</span>
                                    <span class=\"text-xs text-slate-400 font-mono\">{format_duration(e['duration'])}</span>
                                </div>
                            </div>
                            <div class=\"mt-2 text-xs text-slate-600\"><span class=\"font-semibold text-slate-500\">SRS ID:</span> <code class=\"bg-gray-100 px-2 py-0.5 rounded\">{_html_escape(e['srs_id'])}</code></div>
                            {l_error_html}
                        </div>"""

            category_logs_html = f"""<details open class=\"section-card bg-white rounded-2xl shadow-sm border border-gray-200 p-6 group\">
                    <summary class=\"flex items-center justify-between cursor-pointer list-none\">
                        <div class=\"flex items-center gap-2\">
                            <h2 class=\"text-xl font-bold text-gray-800\">Logs</h2>
                            <span class=\"inline-flex items-center rounded-full bg-red-100 px-2.5 py-0.5 text-xs font-semibold text-red-700\">{cat_logs_failed} failed</span>
                            <span class=\"inline-flex items-center rounded-full bg-yellow-100 px-2.5 py-0.5 text-xs font-semibold text-yellow-700\">{cat_logs_skipped} skipped</span>
                        </div>
                        <span class=\"text-xl text-slate-500 transition-transform duration-200 group-open:rotate-180\">▾</span>
                    </summary>
                    <div class=\"mt-4 space-y-3\">
                        {cat_log_rows}
                    </div>
                </details>"""

        cat_html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>{category_name} — Test Dashboard</title>
    <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">
    <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
    <link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap\" rel=\"stylesheet\">
    <script src=\"https://cdn.tailwindcss.com\"></script>
    <script src=\"https://cdn.jsdelivr.net/npm/chart.js@latest\"></script>
    <style>
        * {{ font-family: 'Inter', system-ui, -apple-system, sans-serif; }}
        .sidebar {{ position: fixed; top: 0; left: 0; width: 300px; height: 100vh; background: #ffffff; border-right: 1px solid #e2e8f0; transform: translateX(-100%); transition: transform 0.35s cubic-bezier(0.4, 0, 0.2, 1); z-index: 50; display: flex; flex-direction: column; box-shadow: 4px 0 20px rgba(0,0,0,0.08); }}
        .sidebar.open {{ transform: translateX(0); }}
        .sidebar-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,0.2); backdrop-filter: blur(4px); z-index: 45; opacity: 0; pointer-events: none; transition: opacity 0.35s ease; }}
        .sidebar-overlay.visible {{ opacity: 1; pointer-events: auto; }}
        .sidebar-link.active {{ background: rgba(51,65,85,0.08); border-left: 3px solid #334155; padding-left: 13px; }}
        .sidebar-link.active .sidebar-link-text {{ font-weight: 700; color: #1e293b; }}
        .sidebar-link {{ color: #64748b; }}
        .sidebar-link:hover {{ color: #1e293b; background: #f8fafc; }}
        .sidebar-categories {{ flex: 1; overflow-y: auto; scrollbar-width: thin; scrollbar-color: #cbd5e1 transparent; }}
        .sidebar-categories::-webkit-scrollbar {{ width: 5px; }}
        .sidebar-categories::-webkit-scrollbar-track {{ background: transparent; }}
        .sidebar-categories::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 10px; }}
        .main-wrapper {{ transition: margin-left 0.35s cubic-bezier(0.4, 0, 0.2, 1); }}
        @media (min-width: 1024px) {{ .main-wrapper.shifted {{ margin-left: 300px; }} }}
        .hamburger-btn {{ position: fixed; top: 18px; left: 18px; z-index: 55; width: 44px; height: 44px; border-radius: 12px; background: #ffffff; backdrop-filter: blur(12px); border: 1px solid #e2e8f0; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: all 0.3s ease; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .hamburger-btn:hover {{ background: #f8fafc; transform: scale(1.05); box-shadow: 0 4px 16px rgba(0,0,0,0.12); }}
        .hamburger-btn .bar {{ display: block; width: 20px; height: 2px; background: #334155; border-radius: 2px; transition: all 0.35s ease; }}
        .hamburger-btn .bar + .bar {{ margin-top: 5px; }}
        .hamburger-btn.active .bar:nth-child(1) {{ transform: rotate(45deg) translate(5px, 5px); }}
        .hamburger-btn.active .bar:nth-child(2) {{ opacity: 0; }}
        .hamburger-btn.active .bar:nth-child(3) {{ transform: rotate(-45deg) translate(5px, -5px); }}
        .metric-card {{ background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; padding: 24px; transition: all 0.25s ease; position: relative; overflow: hidden; }}
        .metric-card::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 4px; }}
        .metric-card:hover {{ transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.08); }}
        .metric-card.passed::before {{ background: linear-gradient(90deg, #10B981, #34D399); }}
        .metric-card.failed::before {{ background: linear-gradient(90deg, #EF4444, #F87171); }}
        .metric-card.skipped::before {{ background: linear-gradient(90deg, #F59E0B, #FBBF24); }}
        .metric-card.duration::before {{ background: linear-gradient(90deg, #6366F1, #818CF8); }}
        .progress-ring {{ position: relative; width: 140px; height: 140px; }}
        .progress-ring svg {{ transform: rotate(-90deg); }}
        .progress-ring-text {{ position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }}
        .section-card {{ opacity: 0; transform: translateY(16px); animation: fadeInUp 0.5s ease forwards; }}
        @keyframes fadeInUp {{ to {{ opacity: 1; transform: translateY(0); }} }}
        .section-card:nth-child(1) {{ animation-delay: 0.05s; }}
        .section-card:nth-child(2) {{ animation-delay: 0.1s; }}
        .section-card:nth-child(3) {{ animation-delay: 0.15s; }}
    </style>
</head>
<body class=\"bg-slate-50 text-gray-900 antialiased min-h-screen\">

    <button id=\"hamburgerBtn\" class=\"hamburger-btn\" onclick=\"toggleSidebar()\" aria-label=\"Toggle navigation menu\">
        <div><span class=\"bar\"></span><span class=\"bar\"></span><span class=\"bar\"></span></div>
    </button>

    <div id=\"sidebarOverlay\" class=\"sidebar-overlay\" onclick=\"closeSidebar()\"></div>

    <nav id=\"sidebar\" class=\"sidebar\">
        <div class=\"px-6 pt-16 pb-4 border-b border-slate-200\">
            <h2 class=\"text-slate-800 font-bold text-lg leading-tight mb-4\">Dashboard</h2>
            <div class=\"grid grid-cols-3 gap-2 text-center\">
                <div class=\"rounded-lg py-2 px-1 bg-green-50\">
                    <div class=\"text-green-600 text-sm font-bold\">{metrics['passed']}</div>
                    <div class=\"text-slate-500 text-[10px] uppercase tracking-wider\">Pass</div>
                </div>
                <div class=\"rounded-lg py-2 px-1 bg-red-50\">
                    <div class=\"text-red-600 text-sm font-bold\">{metrics['failed']}</div>
                    <div class=\"text-slate-500 text-[10px] uppercase tracking-wider\">Fail</div>
                </div>
                <div class=\"rounded-lg py-2 px-1 bg-yellow-50\">
                    <div class=\"text-yellow-600 text-sm font-bold\">{metrics['skipped']}</div>
                    <div class=\"text-slate-500 text-[10px] uppercase tracking-wider\">Skip</div>
                </div>
            </div>
        </div>
        <div class=\"px-4 py-3 border-b border-slate-100\">
            <input id=\"sidebarSearch\" type=\"search\" placeholder=\"Search categories...\" class=\"w-full rounded-lg bg-slate-50 border border-slate-200 text-slate-800 placeholder-slate-400 px-3 py-2 text-sm focus:outline-none focus:border-slate-400 focus:ring-2 focus:ring-slate-500/20 transition-all\" />
        </div>
        <div class=\"sidebar-categories px-3 py-3 space-y-1\">
            <div class=\"px-4 py-2\">
                <span id=\"sidebarCategoryCount\" class=\"text-[10px] font-semibold uppercase tracking-widest text-slate-400\">Categories ({len(categories)})</span>
            </div>
            {cat_sidebar_items_html}
            <div id=\"sidebarNoResults\" class=\"hidden px-4 py-3 text-sm text-slate-400 italic\">No categories found</div>
        </div>
        <div class=\"px-5 py-4 border-t border-slate-200 mt-auto space-y-2\">
            <a href=\"../{output_filename}\" class=\"flex items-center gap-2 px-3 py-2.5 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 text-sm font-medium transition-colors\">
                <svg class=\"w-4 h-4\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\" stroke-width=\"2\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M10 19l-7-7m0 0l7-7m-7 7h18\"/></svg>
                <span>Back to Overview</span>
            </a>
        </div>
    </nav>

    <div id=\"mainWrapper\" class=\"main-wrapper\">
        <header class=\"text-white shadow-lg\" style=\"background: linear-gradient(135deg, #0f172a 0%, #111827 50%, #000000 100%);\">
            <div class=\"max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8\">
                <div class=\"flex justify-between items-center\">
                    <div class=\"pl-12 sm:pl-14\">
                        <p class=\"text-white/60 text-xs uppercase tracking-widest mb-1\">Category</p>
                        <h1 class=\"text-2xl sm:text-3xl font-extrabold tracking-tight\">{category_name}</h1>
                        <p class=\"text-white/70 text-sm mt-1\">{category_description}</p>
                    </div>
                    <div class=\"text-right flex items-center gap-4\">
                        <div class=\"hidden sm:block\">
                            <div class=\"flex items-center gap-2 mb-1\">
                                <span class=\"text-sm font-medium text-white/90\">{'All Passing' if cat_fail == 0 else 'Failures Detected'}</span>
                            </div>
                            <p class=\"text-xs text-white/50 uppercase tracking-wider\">Pass Rate: {cat_pass_rate}%</p>
                        </div>
                        <div class=\"bg-white/15 backdrop-blur-sm rounded-xl px-5 py-3 border border-white/20\">
                            <span class=\"text-3xl font-mono font-bold\">{cat_total}</span>
                            <p class=\"text-[10px] text-white/70 uppercase tracking-widest mt-0.5\">Tests</p>
                        </div>
                        <a href=\"../{output_filename}\" class=\"hidden sm:flex items-center gap-2 px-4 py-2.5 rounded-xl bg-white/15 hover:bg-white/25 border border-white/20 text-white text-sm font-medium transition-all\">
                            <svg class=\"w-4 h-4\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\" stroke-width=\"2\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M10 19l-7-7m0 0l7-7m-7 7h18\"/></svg>
                            <span>Overview</span>
                        </a>
                    </div>
                </div>
            </div>
        </header>

        <main class=\"max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8\">
            <div class=\"space-y-8\">
                <div class=\"section-card grid grid-cols-2 lg:grid-cols-4 gap-4\">
                    <div class=\"metric-card passed\">
                        <p class=\"text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1\">Passed</p>
                        <p class=\"text-3xl font-bold text-green-600\">{cat_pass}</p>
                        <p class=\"text-xs text-green-500 mt-1\">{cat_pass_rate}% success</p>
                    </div>
                    <div class=\"metric-card failed\">
                        <p class=\"text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1\">Failed</p>
                        <p class=\"text-3xl font-bold text-red-600\">{cat_fail}</p>
                        <p class=\"text-xs text-red-400 mt-1\">{round((cat_fail / cat_total) * 100, 1) if cat_total > 0 else 0}% failure</p>
                    </div>
                    <div class=\"metric-card skipped\">
                        <p class=\"text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1\">Skipped</p>
                        <p class=\"text-3xl font-bold text-yellow-600\">{cat_skip}</p>
                        <p class=\"text-xs text-yellow-500 mt-1\">{round((cat_skip / cat_total) * 100, 1) if cat_total > 0 else 0}% skipped</p>
                    </div>
                    <div class=\"metric-card duration\">
                        <p class=\"text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1\">Duration</p>
                        <p class=\"text-3xl font-bold text-indigo-600\">{format_duration(cat_duration)}</p>
                        <p class=\"text-xs text-indigo-400 mt-1\">category runtime</p>
                    </div>
                </div>

                <div class=\"section-card grid grid-cols-1 lg:grid-cols-2 gap-6\">
                    <div class=\"bg-white rounded-2xl shadow-sm border border-gray-200 p-6\">
                        <h2 class=\"text-lg font-bold text-gray-800 mb-4\">Test Results Distribution</h2>
                        <div class=\"relative max-w-[200px] mx-auto\">
                            <canvas id=\"{chart_id}\"></canvas>
                        </div>
                    </div>
                    <div class=\"bg-white rounded-2xl shadow-sm border border-gray-200 p-6\">
                        <h2 class=\"text-lg font-bold text-gray-800 mb-4\">Pass Rate</h2>
                        <div class=\"progress-ring mx-auto\">
                            <svg width=\"140\" height=\"140\" viewBox=\"0 0 140 140\">
                                <circle cx=\"70\" cy=\"70\" r=\"60\" stroke=\"#e2e8f0\" stroke-width=\"10\" fill=\"none\"></circle>
                                <circle cx=\"70\" cy=\"70\" r=\"60\" stroke=\"url(#passGradCat)\" stroke-width=\"10\" fill=\"none\"
                                        stroke-dasharray=\"{round(2 * 3.14159 * 60 * cat_pass_rate / 100, 1)} {round(2 * 3.14159 * 60, 1)}\"
                                        stroke-linecap=\"round\"></circle>
                                <defs><linearGradient id=\"passGradCat\" x1=\"0%\" y1=\"0%\" x2=\"100%\" y2=\"0%\">
                                    <stop offset=\"0%\" stop-color=\"#64748b\"/><stop offset=\"100%\" stop-color=\"#0f172a\"/>
                                </linearGradient></defs>
                            </svg>
                            <div class=\"progress-ring-text\">
                                <span class=\"text-2xl font-bold text-gray-800\">{cat_pass_rate}%</span>
                                <span class=\"text-xs text-slate-500 mt-1\">Pass Rate</span>
                            </div>
                        </div>
                    </div>
                </div>

                {category_coverage_html}

                {category_logs_html}

                <section class=\"section-card bg-white rounded-2xl shadow-sm border border-gray-200 p-6\">
                    <div class=\"flex flex-col sm:flex-row justify-between items-start sm:items-center mb-6 gap-4\">
                        <h2 class=\"text-xl font-bold text-gray-800\">Tests in {category_name}</h2>
                        <div class=\"flex items-center gap-2 text-xs text-slate-500\">
                            <svg class=\"w-4 h-4\" fill=\"none\" viewBox=\"0 0 24 24\" stroke=\"currentColor\" stroke-width=\"1.5\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5\"/></svg>
                            <span>{cat_total} tests</span>
                        </div>
                    </div>
                    <div class=\"mb-6 grid gap-4 md:grid-cols-3 items-end\">
                        <label class=\"block\">
                            <span class=\"text-xs font-semibold text-slate-500 uppercase tracking-wider\">Search tests</span>
                            <input id=\"testSearch\" type=\"search\" class=\"mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all\" placeholder=\"Search test names...\" />
                        </label>
                        <label class=\"block\">
                            <span class=\"text-xs font-semibold text-slate-500 uppercase tracking-wider\">Filter status</span>
                            <select id=\"statusFilter\" class=\"mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all\">
                                <option value=\"all\">All</option>
                                <option value=\"passed\">Passed only</option>
                                <option value=\"failed\">Failed only</option>
                                <option value=\"skipped\">Skipped only</option>
                            </select>
                        </label>
                        <label class=\"block\">
                            <span class=\"text-xs font-semibold text-slate-500 uppercase tracking-wider\">Sort order</span>
                            <select id=\"sortOrder\" class=\"mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all\">
                                <option value=\"name-asc\">Name ascending</option>
                                <option value=\"name-desc\">Name descending</option>
                            </select>
                        </label>
                    </div>
                    <div id=\"categoryTests\" class=\"space-y-4\">
                        {category_test_entries_html}
                    </div>
                    <div id=\"categoryTestsNoResults\" class=\"hidden text-center py-12 text-slate-400\">
                        <p class=\"text-lg font-medium\">No matching tests found</p>
                        <p class=\"text-sm mt-1\">Try adjusting your search or filter criteria.</p>
                    </div>
                </section>
            </div>
        </main>

        <footer class=\"mt-8 py-8 border-t border-gray-200\">
            <div class=\"max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center text-sm text-slate-500\">Automated Test Report</div>
        </footer>
    </div>

    <script>
        let sidebarOpen = false;
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebarOverlay');
        const hamburger = document.getElementById('hamburgerBtn');
        const mainWrapper = document.getElementById('mainWrapper');
        function toggleSidebar() {{
            sidebarOpen = !sidebarOpen;
            sidebar.classList.toggle('open', sidebarOpen);
            overlay.classList.toggle('visible', sidebarOpen);
            hamburger.classList.toggle('active', sidebarOpen);
            document.body.style.overflow = sidebarOpen ? 'hidden' : '';
            if (window.innerWidth >= 1024) {{ mainWrapper.classList.toggle('shifted', sidebarOpen); }}
        }}
        function closeSidebar() {{
            sidebarOpen = false;
            sidebar.classList.remove('open');
            overlay.classList.remove('visible');
            hamburger.classList.remove('active');
            mainWrapper.classList.remove('shifted');
            document.body.style.overflow = '';
        }}
        document.getElementById('sidebarSearch').addEventListener('input', function() {{
            const query = this.value.toLowerCase().trim();
            let visibleCount = 0;
            document.querySelectorAll('.sidebar-link').forEach(link => {{
                const text = link.querySelector('.sidebar-link-text').textContent.toLowerCase();
                const match = !query || text.includes(query);
                link.style.display = match ? '' : 'none';
                if (match) visibleCount++;
            }});
            const countEl = document.getElementById('sidebarCategoryCount');
            if (countEl) countEl.textContent = 'Categories (' + visibleCount + ')';
            const noResults = document.getElementById('sidebarNoResults');
            if (noResults) noResults.classList.toggle('hidden', visibleCount !== 0);
        }});

        new Chart(document.getElementById('{chart_id}').getContext('2d'), {{
            type: 'doughnut',
            data: {{
                labels: {json.dumps(['Passed', 'Failed', 'Skipped'])},
                datasets: [{{
                    data: {json.dumps([cat_pass, cat_fail, cat_skip])},
                    backgroundColor: ['#10B981','#EF4444','#F59E0B'],
                    borderWidth: 3, borderColor: '#ffffff', hoverOffset: 6
                }}]
            }},
            options: {{ responsive: true, cutout: '65%', plugins: {{ legend: {{ position: 'bottom', labels: {{ padding: 16, usePointStyle: true, pointStyle: 'circle' }} }} }} }}
        }});

        function filterCategoryTests() {{
            const searchValue = document.getElementById('testSearch').value.toLowerCase().trim();
            const statusValue = document.getElementById('statusFilter').value;
            const sortValue = document.getElementById('sortOrder').value;
            const container = document.getElementById('categoryTests');
            const entries = Array.from(container.querySelectorAll('.test-entry'));
            entries.forEach(entry => {{
                const name = entry.dataset.name || '';
                const status = entry.dataset.status || '';
                const matchesSearch = !searchValue || name.includes(searchValue);
                const matchesStatus = statusValue === 'all' || status === statusValue;
                entry.style.display = matchesSearch && matchesStatus ? '' : 'none';
            }});
            const visibleEntries = entries.filter(entry => entry.style.display !== 'none');
            visibleEntries.sort((a, b) => {{
                const nameA = a.dataset.name || '';
                const nameB = b.dataset.name || '';
                return sortValue === 'name-desc' ? nameB.localeCompare(nameA) : nameA.localeCompare(nameB);
            }});
            visibleEntries.forEach(entry => container.appendChild(entry));
            const noResults = document.getElementById('categoryTestsNoResults');
            if (noResults) noResults.classList.toggle('hidden', visibleEntries.length !== 0);
        }}
        document.getElementById('testSearch').addEventListener('input', filterCategoryTests);
        document.getElementById('statusFilter').addEventListener('change', filterCategoryTests);
        document.getElementById('sortOrder').addEventListener('change', filterCategoryTests);
        window.addEventListener('load', filterCategoryTests);
    </script>
</body>
</html>
"""
        try:
            category_path.write_text(cat_html, encoding='utf-8')
        except Exception:
            pass

    # Build sidebar menu items
    sidebar_items_html = ""
    for idx, (category_name, test_cases) in enumerate(categories.items()):
        slug = re.sub(r'[^a-z0-9]+', '-', category_name.lower()).strip('-')
        cat_id = f"cat-{slug}"
        pass_ct = sum(1 for t in test_cases if t["status"] == "passed")
        fail_ct = sum(1 for t in test_cases if t["status"] == "failed")
        skip_ct = sum(1 for t in test_cases if t["status"] == "skipped")
        total_ct = len(test_cases)
        # Status dot color
        if fail_ct > 0:
            dot_color = "#EF4444"
        elif skip_ct > 0:
            dot_color = "#F59E0B"
        else:
            dot_color = "#10B981"
        category_filename = f"category-{slug}.html"
        sidebar_items_html += f"""
            <a href="categories/{category_filename}" data-cat-id="{cat_id}"
               class="sidebar-link group flex items-center gap-3 px-4 py-3 rounded-lg text-sm transition-all duration-200 hover:bg-white/10">
                <span class="flex-shrink-0 w-2.5 h-2.5 rounded-full" style="background:{dot_color};"></span>
                <span class="flex-1 truncate sidebar-link-text">{category_name}</span>
                <span class="flex-shrink-0 text-xs opacity-60 font-mono">{pass_ct}/{total_ct}</span>
            </a>"""

    # Pass rate percentage
    pass_rate = round((metrics['passed'] / metrics['total']) * 100, 1) if metrics['total'] > 0 else 0

    # HTML Framework Template
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Test Execution Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@latest"></script>
    <style>
        * {{ font-family: 'Inter', system-ui, -apple-system, sans-serif; }}

        /* ── Sidebar ── */
        .sidebar {{
            position: fixed;
            top: 0;
            left: 0;
            width: 300px;
            height: 100vh;
            background: #ffffff;
            border-right: 1px solid #e2e8f0;
            transform: translateX(-100%);
            transition: transform 0.35s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 50;
            display: flex;
            flex-direction: column;
            box-shadow: 4px 0 20px rgba(0,0,0,0.08);
        }}
        .sidebar.open {{
            transform: translateX(0);
        }}
        .sidebar-overlay {{
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.2);
            backdrop-filter: blur(4px);
            z-index: 45;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.35s ease;
        }}
        .sidebar-overlay.visible {{
            opacity: 1;
            pointer-events: auto;
        }}
        .sidebar-link.active {{
            background: rgba(51,65,85,0.08);
            border-left: 3px solid #334155;
            padding-left: 13px;
        }}
        .sidebar-link.active .sidebar-link-text {{
            font-weight: 700;
            color: #1e293b;
        }}
        .sidebar-link {{
            color: #64748b;
        }}
        .sidebar-link:hover {{
            color: #1e293b;
            background: #f8fafc;
        }}
        .sidebar-categories {{
            flex: 1;
            overflow-y: auto;
            scrollbar-width: thin;
            scrollbar-color: #cbd5e1 transparent;
        }}
        .sidebar-categories::-webkit-scrollbar {{
            width: 5px;
        }}
        .sidebar-categories::-webkit-scrollbar-track {{
            background: transparent;
        }}
        .sidebar-categories::-webkit-scrollbar-thumb {{
            background: #cbd5e1;
            border-radius: 10px;
        }}

        /* ── Main content shift ── */
        .main-wrapper {{
            transition: margin-left 0.35s cubic-bezier(0.4, 0, 0.2, 1);
        }}
        @media (min-width: 1024px) {{
            .main-wrapper.shifted {{
                margin-left: 300px;
            }}
        }}

        /* ── Hamburger button ── */
        .hamburger-btn {{
            position: fixed;
            top: 18px;
            left: 18px;
            z-index: 55;
            width: 44px;
            height: 44px;
            border-radius: 12px;
            background: #ffffff;
            backdrop-filter: blur(12px);
            border: 1px solid #e2e8f0;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .hamburger-btn:hover {{
            background: #f8fafc;
            transform: scale(1.05);
            box-shadow: 0 4px 16px rgba(0,0,0,0.12);
        }}
        .hamburger-btn .bar {{
            display: block;
            width: 20px;
            height: 2px;
            background: #334155;
            border-radius: 2px;
            transition: all 0.35s ease;
        }}
        .hamburger-btn .bar + .bar {{
            margin-top: 5px;
        }}
        .hamburger-btn.active .bar:nth-child(1) {{
            transform: rotate(45deg) translate(5px, 5px);
        }}
        .hamburger-btn.active .bar:nth-child(2) {{
            opacity: 0;
        }}
        .hamburger-btn.active .bar:nth-child(3) {{
            transform: rotate(-45deg) translate(5px, -5px);
        }}

        /* ── Metric cards ── */
        .metric-card {{
            background: #ffffff;
            border-radius: 16px;
            border: 1px solid #e2e8f0;
            padding: 24px;
            transition: all 0.25s ease;
            position: relative;
            overflow: hidden;
        }}
        .metric-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
        }}
        .metric-card:hover {{
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.08);
        }}
        .metric-card.passed::before {{ background: linear-gradient(90deg, #10B981, #34D399); }}
        .metric-card.failed::before {{ background: linear-gradient(90deg, #EF4444, #F87171); }}
        .metric-card.skipped::before {{ background: linear-gradient(90deg, #F59E0B, #FBBF24); }}
        .metric-card.duration::before {{ background: linear-gradient(90deg, #6366F1, #818CF8); }}
        .metric-card.total::before {{ background: linear-gradient(90deg, #0EA5E9, #38BDF8); }}

        /* ── Progress ring ── */
        .progress-ring {{
            position: relative;
            width: 160px;
            height: 160px;
        }}
        .progress-ring svg {{
            transform: rotate(-90deg);
        }}
        .progress-ring-text {{
            position: absolute;
            inset: 0;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }}

        /* ── Category section highlight ── */
        .category-section {{
            scroll-margin-top: 100px;
        }}

        /* ── Smooth section reveal ── */
        .section-card {{
            opacity: 0;
            transform: translateY(16px);
            animation: fadeInUp 0.5s ease forwards;
        }}
        @keyframes fadeInUp {{
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .section-card:nth-child(1) {{ animation-delay: 0.05s; }}
        .section-card:nth-child(2) {{ animation-delay: 0.1s; }}
        .section-card:nth-child(3) {{ animation-delay: 0.15s; }}
        .section-card:nth-child(4) {{ animation-delay: 0.2s; }}
    </style>
</head>
<body class="bg-slate-50 text-gray-900 antialiased min-h-screen">

    <!-- ═══ Hamburger Toggle Button ═══ -->
    <button id="hamburgerBtn" class="hamburger-btn" onclick="toggleSidebar()" aria-label="Toggle navigation menu">
        <div>
            <span class="bar"></span>
            <span class="bar"></span>
            <span class="bar"></span>
        </div>
    </button>

    <!-- ═══ Sidebar Overlay ═══ -->
    <div id="sidebarOverlay" class="sidebar-overlay" onclick="closeSidebar()"></div>

    <!-- ═══ Sidebar Navigation ═══ -->
    <nav id="sidebar" class="sidebar">
        <!-- Sidebar Header -->
        <div class="px-6 pt-16 pb-4 border-b border-slate-200">
            <h2 class="text-slate-800 font-bold text-lg leading-tight mb-4">Dashboard</h2>
            <!-- Quick Stats in Sidebar -->
            <div class="grid grid-cols-3 gap-2 text-center">
                <div class="rounded-lg py-2 px-1 bg-green-50">
                    <div class="text-green-600 text-sm font-bold">{metrics['passed']}</div>
                    <div class="text-slate-500 text-[10px] uppercase tracking-wider">Pass</div>
                </div>
                <div class="rounded-lg py-2 px-1 bg-red-50">
                    <div class="text-red-600 text-sm font-bold">{metrics['failed']}</div>
                    <div class="text-slate-500 text-[10px] uppercase tracking-wider">Fail</div>
                </div>
                <div class="rounded-lg py-2 px-1 bg-yellow-50">
                    <div class="text-yellow-600 text-sm font-bold">{metrics['skipped']}</div>
                    <div class="text-slate-500 text-[10px] uppercase tracking-wider">Skip</div>
                </div>
            </div>
        </div>
        <!-- Sidebar Search -->
        <div class="px-4 py-3 border-b border-slate-100">
                 <input id="sidebarSearch" type="search" placeholder="Search categories..."
                     class="w-full rounded-lg bg-slate-50 border border-slate-200 text-slate-800 placeholder-slate-400 px-3 py-2 text-sm focus:outline-none focus:border-slate-400 focus:ring-2 focus:ring-slate-500/20 transition-all" />
        </div>
        <!-- Sidebar Category List (scrollable) -->
        <div class="sidebar-categories px-3 py-3 space-y-1">
            <div class="px-4 py-2">
                <span id="sidebarCategoryCount" class="text-[10px] font-semibold uppercase tracking-widest text-slate-400">Categories ({len(categories)})</span>
            </div>
            {sidebar_items_html}
            <div id="sidebarNoResults" class="hidden px-4 py-3 text-sm text-slate-400 italic">No categories found</div>
        </div>
        <!-- Sidebar Footer -->
        <div class="px-6 py-4 border-t border-slate-200 mt-auto">
            <div class="flex items-center gap-2 text-slate-400 text-xs">
                <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                <span>Generated {datetime.now().strftime('%b %d, %Y at %H:%M')}</span>
            </div>
        </div>
    </nav>

    <!-- ═══ Main Wrapper ═══ -->
    <div id="mainWrapper" class="main-wrapper">

        <!-- ═══ Top Header Bar ═══ -->
        <header class="text-white shadow-lg" style="background: linear-gradient(135deg, #0f172a 0%, #111827 50%, #000000 100%);">
            <div class="max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
                <div class="flex justify-between items-center">
                    <div class="pl-12 sm:pl-14">
                        <h1 class="text-2xl sm:text-3xl font-extrabold tracking-tight">XCTest Automated Report</h1>
                        <p class="text-white/70 text-sm mt-1">Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                        {header_device_html}
                    </div>
                    <div class="text-right flex items-center gap-6">
                        <div class="hidden sm:block">
                            <div class="flex items-center gap-2 mb-1">
                                <span class="text-sm font-medium text-white/90">{'All Passing' if metrics['failed'] == 0 else 'Failures Detected'}</span>
                            </div>
                            <p class="text-xs text-white/50 uppercase tracking-wider">Pass Rate: {pass_rate}%</p>
                        </div>
                        <div class="bg-white/15 backdrop-blur-sm rounded-xl px-5 py-3 border border-white/20">
                            <span class="text-3xl font-mono font-bold">{metrics['total']}</span>
                            <p class="text-[10px] text-white/70 uppercase tracking-widest mt-0.5">Total Tests</p>
                        </div>
                    </div>
                </div>
            </div>
        </header>

        <!-- ═══ Dashboard Content ═══ -->
        <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
            <div class="space-y-8">

                <!-- ── Row 1: Metric Cards ── -->
                <div class="section-card grid grid-cols-2 lg:grid-cols-5 gap-4">
                    <div class="metric-card total">
                        <p class="text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1">Total Tests</p>
                        <p class="text-3xl font-bold text-slate-800">{metrics['total']}</p>
                        <p class="text-xs text-slate-400 mt-1">{len(categories)} categories</p>
                    </div>
                    <div class="metric-card passed">
                        <p class="text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1">Passed</p>
                        <p class="text-3xl font-bold text-green-600">{metrics['passed']}</p>
                        <p class="text-xs text-green-500 mt-1">{pass_rate}% success</p>
                    </div>
                    <div class="metric-card failed">
                        <p class="text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1">Failed</p>
                        <p class="text-3xl font-bold text-red-600">{metrics['failed']}</p>
                        <p class="text-xs text-red-400 mt-1">{round((metrics['failed'] / metrics['total']) * 100, 1) if metrics['total'] > 0 else 0}% failure</p>
                    </div>
                    <div class="metric-card skipped">
                        <p class="text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1">Skipped</p>
                        <p class="text-3xl font-bold text-yellow-600">{metrics['skipped']}</p>
                        <p class="text-xs text-yellow-500 mt-1">{round((metrics['skipped'] / metrics['total']) * 100, 1) if metrics['total'] > 0 else 0}% skipped</p>
                    </div>
                    <div class="metric-card duration">
                        <p class="text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1">Duration</p>
                        <p class="text-3xl font-bold text-indigo-600">{format_duration(metrics.get('wall_duration') or metrics['duration'])}</p>
                        <p class="text-xs text-indigo-400 mt-1">total runtime</p>
                    </div>
                </div>

                <!-- ── Row 2: Charts ── -->
                <div class="section-card grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
                        <h2 class="text-lg font-bold text-gray-800 mb-4">Test Results Distribution</h2>
                        <div class="relative max-w-[220px] mx-auto">
                            <canvas id="metricsChart"></canvas>
                        </div>
                    </div>
                    <div class="bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
                        <h2 class="text-lg font-bold text-gray-800 mb-4">Pass Rate</h2>
                        <div class="progress-ring mx-auto">
                            <svg width="160" height="160" viewBox="0 0 160 160">
                                <circle cx="80" cy="80" r="70" stroke="#e2e8f0" stroke-width="12" fill="none"></circle>
                                <circle cx="80" cy="80" r="70" stroke="url(#passGrad)" stroke-width="12" fill="none"
                                        stroke-dasharray="{round(2 * 3.14159 * 70 * pass_rate / 100, 1)} {round(2 * 3.14159 * 70, 1)}"
                                        stroke-linecap="round"></circle>
                                <defs><linearGradient id="passGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                                    <stop offset="0%" stop-color="#64748b"/><stop offset="100%" stop-color="#0f172a"/>
                                </linearGradient></defs>
                            </svg>
                            <div class="progress-ring-text">
                                <span class="text-3xl font-bold text-gray-800">{pass_rate}%</span>
                                <span class="text-xs text-slate-500 mt-1">Pass Rate</span>
                            </div>
                        </div>
                        <!-- Legend 
                        <!-- 
                        <div class="flex justify-center gap-6 mt-6 text-sm text-slate-600">
                            <div class="flex items-center gap-2"><span class="w-3 h-3 rounded-full bg-green-500"></span> Passed</div>
                            <div class="flex items-center gap-2"><span class="w-3 h-3 rounded-full bg-red-500"></span> Failed</div>
                            <div class="flex items-center gap-2"><span class="w-3 h-3 rounded-full bg-yellow-500"></span> Skipped</div>
                        </div>
                        -->
                    </div>
                </div>

                {insights_overview_html if insights_overview_html else ''}

                {extra_overview_html if extra_overview_html else ''}

                {logs_overview_html if logs_overview_html else ''}

                <!-- ── Row 3: Category Breakdown ── -->
                <section class="section-card bg-white rounded-2xl shadow-sm border border-gray-200 p-6">
                    <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center mb-6 gap-4">
                        <h2 class="text-xl font-bold text-gray-800">Test Categories & Breakdown</h2>
                        <div class="flex items-center gap-2 text-xs text-slate-500">
                            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5"/></svg>
                            <span>{len(categories)} categories</span>
                        </div>
                    </div>
                    <div class="mb-6 grid gap-4 md:grid-cols-3 items-end">
                        <label class="block">
                            <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Search categories</span>
                            <input id="categorySearch" type="search" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all" placeholder="Search category names..." />
                        </label>
                        <label class="block">
                            <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Filter status</span>
                            <select id="categoryStatusFilter" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all">
                                <option value="all">All</option>
                                <option value="passed-only">Passed only</option>
                                <option value="failed-only">Failed only</option>
                                <option value="mixed">Both</option>
                            </select>
                        </label>
                        <label class="block">
                            <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Sort order</span>
                            <select id="categorySortOrder" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500/30 focus:border-slate-500 transition-all">
                                <option value="name-asc">Name ascending</option>
                                <option value="name-desc">Name descending</option>
                            </select>
                        </label>
                    </div>
                    <div id="categoriesContainer">
                        {accordion_html if accordion_html else '<p class="text-gray-500">No structured categories found. Verify test runs bundle details.</p>'}
                    </div>
                    <div id="categoriesNoResults" class="hidden text-center py-12 text-slate-400">
                        <p class="text-lg font-medium">No matching categories found</p>
                        <p class="text-sm mt-1">Try adjusting your search or filter criteria.</p>
                    </div>
                </section>
            </div>
        </main>

        <footer class="mt-8 py-8 border-t border-gray-200">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center text-sm text-slate-500">Automated Test Report</div>
        </footer>
    </div>

    <!-- ═══ Scripts ═══ -->
    <script>
        /* ── Sidebar toggle ── */
        let sidebarOpen = false;
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebarOverlay');
        const hamburger = document.getElementById('hamburgerBtn');
        const mainWrapper = document.getElementById('mainWrapper');

        function toggleSidebar() {{
            sidebarOpen = !sidebarOpen;
            sidebar.classList.toggle('open', sidebarOpen);
            overlay.classList.toggle('visible', sidebarOpen);
            hamburger.classList.toggle('active', sidebarOpen);
            document.body.style.overflow = sidebarOpen ? 'hidden' : '';
            if (window.innerWidth >= 1024) {{
                mainWrapper.classList.toggle('shifted', sidebarOpen);
            }}
        }}
        function closeSidebar() {{
            sidebarOpen = false;
            sidebar.classList.remove('open');
            overlay.classList.remove('visible');
            hamburger.classList.remove('active');
            mainWrapper.classList.remove('shifted');
            document.body.style.overflow = '';
        }}




        /* ── Sidebar search filter ── */
        document.getElementById('sidebarSearch').addEventListener('input', function() {{
            const query = this.value.toLowerCase().trim();
            let visibleCount = 0;
            document.querySelectorAll('.sidebar-link').forEach(link => {{
                const text = link.querySelector('.sidebar-link-text').textContent.toLowerCase();
                const match = !query || text.includes(query);
                link.style.display = match ? '' : 'none';
                if (match) visibleCount++;
            }});
            const countEl = document.getElementById('sidebarCategoryCount');
            if (countEl) countEl.textContent = 'Categories (' + visibleCount + ')';
            const noResults = document.getElementById('sidebarNoResults');
            if (noResults) noResults.classList.toggle('hidden', visibleCount !== 0);
        }});

        /* ── Charts ── */
        const ctx = document.getElementById('metricsChart').getContext('2d');
        new Chart(ctx, {{
            type: 'doughnut',
            data: {{
                labels: {json.dumps(chart_labels)},
                datasets: [{{
                    data: {json.dumps(chart_data)},
                    backgroundColor: ['#10B981', '#EF4444', '#F59E0B'],
                    borderWidth: 3,
                    borderColor: '#ffffff',
                    hoverOffset: 6
                }}]
            }},
            options: {{
                responsive: true,
                cutout: '65%',
                plugins: {{
                    legend: {{ position: 'bottom', labels: {{ padding: 16, usePointStyle: true, pointStyle: 'circle' }} }}
                }}
            }}
        }});

        /* ── Category filter/sort ── */
        function filterAndSortCategories() {{
            const searchValue = document.getElementById('categorySearch').value.toLowerCase().trim();
            const statusValue = document.getElementById('categoryStatusFilter').value;
            const sortValue = document.getElementById('categorySortOrder').value;
            const container = document.getElementById('categoriesContainer');
            const cards = Array.from(container.querySelectorAll('.category-card'));

            cards.forEach(card => {{
                const name = card.getAttribute('data-category-name') || '';
                const status = card.getAttribute('data-category-status') || 'mixed';
                const matchesSearch = !searchValue || name.includes(searchValue);
                const matchesStatus = statusValue === 'all' || status === statusValue;
                card.style.display = matchesSearch && matchesStatus ? '' : 'none';
            }});

            const visibleCards = cards.filter(card => card.style.display !== 'none');
            visibleCards.sort((a, b) => {{
                const nameA = a.getAttribute('data-category-name') || '';
                const nameB = b.getAttribute('data-category-name') || '';
                return sortValue === 'name-desc' ? nameB.localeCompare(nameA) : nameA.localeCompare(nameB);
            }});
            visibleCards.forEach(card => container.appendChild(card));
            const noResults = document.getElementById('categoriesNoResults');
            if (noResults) noResults.classList.toggle('hidden', visibleCards.length !== 0);
        }}

        document.getElementById('categorySearch').addEventListener('input', filterAndSortCategories);
        document.getElementById('categoryStatusFilter').addEventListener('change', filterAndSortCategories);
        document.getElementById('categorySortOrder').addEventListener('change', filterAndSortCategories);
        window.addEventListener('load', filterAndSortCategories);

        /* ── Highlight active category on scroll ── */
        const catSections = document.querySelectorAll('.category-section');
        const sidebarLinks = document.querySelectorAll('.sidebar-link');
        if (catSections.length > 0) {{
            const observer = new IntersectionObserver((entries) => {{
                entries.forEach(entry => {{
                    if (entry.isIntersecting) {{
                        const id = entry.target.id.replace('section-', '');
                        sidebarLinks.forEach(l => {{
                            const linkCatId = l.getAttribute('data-cat-id');
                            l.classList.toggle('active', linkCatId === id);
                        }});
                    }}
                }});
            }}, {{ threshold: 0.3 }});
            catSections.forEach(s => observer.observe(s));
        }}
    </script>
</body>
</html>
"""

    output_path = output_dir / output_filename
    with open(output_path, "w") as f:
        f.write(html_content)
    print(f"✓ Report generated successfully: {output_path}")

    # Write a simple file structure manifest and detect coverage files in current bundle
    manifest_path = output_dir / "file_structure.txt"
    try:
        files = [str(p.relative_to(Path.cwd())) for p in output_dir.rglob('*') if p.is_file()]
        manifest_text = "Generated files:\n" + "\n".join(files)
        manifest_path.write_text(manifest_text, encoding='utf-8')
        print(f"✓ File structure manifest: {manifest_path}")
    except Exception:
        pass



