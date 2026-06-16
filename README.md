# xchtml

Generate beautiful, shareable HTML test reports from Xcode `.xcresult` bundles.

![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Created by Gram.

## Install

### Homebrew (recommended)

```bash
brew install igram7/xchtml
```

### From source

```bash
git clone https://github.com/igram7/xchtml.git
cd xchtml
pip install .
```

## Usage

```bash
# Auto-detect the latest xcresult from Xcode DerivedData
xchtml generate

# Specify a bundle path
xchtml generate TestReport.xcresult
xchtml generate /path/to/MyRun.xcresult

# Custom output directory
xchtml generate TestReport.xcresult -o ./my-reports

# Show version
xchtml --version
```

### Legacy mode flags

For backward compatibility with the original script:

```bash
xchtml generate --mode auto
xchtml generate --mode manual --xcresult TestReport.xcresult
```

## Output

After a successful run, generated files are written under the output directory (default: `reports/`):

```text
reports/
├── report.html              # Main overview dashboard
└── categories/
    ├── category-*.html      # Per-category detail pages
    ├── logs.html            # Failure/skipped log aggregation
    └── coverage.html        # Code coverage breakdown
```

Open `reports/report.html` in a browser to explore the full dashboard.

## Features

- **Auto mode**: Automatically finds the latest `.xcresult` from Xcode DerivedData
- **Manual mode**: Target any specific `.xcresult` path or bundle name
- **Rich test summary**: passed, failed, skipped, duration, pass rate
- **Category breakdown** with drill-down pages
- **Failure/skipped log** aggregation page
- **Coverage summary** and file-level coverage views
- **Top Insights** support from xcresult summary when available
- **Zero external dependencies**: only Python stdlib modules

## Requirements

- macOS
- Python 3.8+
- Xcode command line tools:

```bash
xcrun xcresulttool version
xcrun xccov --help
```

If not installed:

```bash
xcode-select --install
```

## Typical Workflow

1. Run tests in Xcode
2. Generate report:

```bash
xchtml generate
```

3. Open `reports/report.html`
4. Share the generated HTML artifacts as needed

## Project Structure

```text
src/xchtml/
├── __init__.py    # Package version
├── cli.py         # CLI entry point (xchtml command)
└── core.py        # Report generation logic
```

## Troubleshooting

### No .xcresult bundle found in Xcode DerivedData

- Ensure tests were run from Xcode on this machine
- Provide the path directly: `xchtml generate <path>.xcresult`

### xcresulttool or xccov command issues

- Reinstall or reconfigure Xcode command line tools:

```bash
xcode-select --install
```

## License

MIT — see [LICENSE](LICENSE) for details.
