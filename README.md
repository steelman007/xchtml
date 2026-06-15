# XCTest HTML Report Generator(xchtml)

Generate a clean, shareable HTML dashboard from an Xcode .xcresult bundle.

This tool parses test results, metadata, logs, and coverage to produce:

- A main report overview page
- Per-category report pages
- A dedicated logs page
- A dedicated coverage page

Created by Gram.

## Features

- Manual mode: run against a specific .xcresult path or bundle name
- Auto mode: automatically finds the latest .xcresult from Xcode DerivedData
- Rich test summary: passed, failed, skipped, duration, pass rate
- Category breakdown with drill-down pages
- Failure/skipped log aggregation page
- Coverage summary and file-level coverage views
- Top Insights support from xcresult summary when available

## Requirements

- macOS
- Python 3.8+
- Xcode command line tools available
- Access to:
  - xcrun xcresulttool
  - xcrun xccov

You can verify required tools:

```bash
xcrun xcresulttool version
xcrun xccov --help
```

## Project Structure

Key files and folders:

- xchtml.py: main script
- reports/report.html: generated overview report
- reports/categories/: generated category pages plus logs and coverage pages

## Setup

1. Clone or download this project.
2. Open Terminal in the script directory.
3. Make sure Xcode command line tools are configured.

```bash
cd htmlReportGenerationTool
```

No external Python packages are required.

## Usage

### 1) Manual Mode

Use manual mode when you want to target a specific .xcresult bundle.

```bash
python3 xchtml.py --mode manual --xcresult TestReport.xcresult
```

You can pass:

- Bundle name
- Relative path
- Absolute path

Examples:

```bash
python3 xchtml.py --mode manual --xcresult ./TestReport.xcresult
python3 xchtml.py --mode manual --xcresult /path/to/MyRun.xcresult
```

### 2) Auto Mode

Use auto mode to process the most recently modified .xcresult from Xcode DerivedData.

```bash
python3 xchtml.py --mode auto
```

Auto mode scans:

```text
~/Library/Developer/Xcode/DerivedData
```

## Output

After a successful run, generated files are written under:

```text
reports/
```

Main entry point:

```text
reports/report.html
```

Open it in a browser to explore the full dashboard.

## Typical Workflow

1. Run tests in Xcode.
2. Generate report in auto mode:

```bash
python3 xchtml.py --mode auto
```

3. Open reports/report.html.
4. Share generated HTML artifacts as needed.

## Troubleshooting

### Error: No .xcresult bundle found in Xcode DerivedData

- Ensure tests were run from Xcode on this machine.
- Use manual mode and pass the exact .xcresult path.

### Error: manual mode requires --xcresult

- Provide the --xcresult argument:

```bash
python3 xchtml.py --mode manual --xcresult <your_bundle>.xcresult
```

### xcresulttool or xccov command issues

- Reinstall or reconfigure Xcode command line tools:

```bash
xcode-select --install
```

## Notes

- This repository intentionally uses public-safe branding in generated pages.
- The script filename is xchtml.py.
