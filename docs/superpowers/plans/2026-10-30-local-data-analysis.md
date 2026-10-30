# Local table analysis implementation plan

Goal: add two project skills and four tools for local CSV/XLSX analysis.
Architecture: workspace-bound tool factory; immutable dataset/result JSON artifacts; Decimal aggregation; pandas date grouping; openpyxl Excel IO; matplotlib PNG output.
Execution: inline implementation, no commits. Existing user changes remain intact.

- [x] Write fixture-based checks for exact totals, file boundaries, malformed data, Excel, chart and export.
- [x] Implement inspect_table / analyze_table / plot_analysis / export_report; register against workspace_root.
- [x] Add local-data-analysis and analysis-reporting skills, examples and project rules.
- [x] Run related offline tests, then existing regression suite; inspect generated chart and report.

Constraints: original files stay unchanged; only data/inputs sources; each result has provenance; no eval or arbitrary code execution; limits of 10 MiB, 100000 rows and 100 columns; ambiguous sheets/headers/numeric/date values return errors. No live model or network data requests during validation.
Initial operations: filters, explicit deduplication, sum/mean/min/max/count, grouping, monthly/daily/yearly date grouping, sorting/top-N and optional many-to-one table join. Output CSV/XLSX/Markdown plus PNG. Numeric totals are decimal strings, chart conversion is presentation-only. Legacy XLS and Excel formula evaluation are excluded.

Validation: new tests 21 passed; regression suite before final edge-case additions 99 passed plus 8 subtests; final edge cases verified in targeted suite. 171 installed packages compatible. Offline Agent graph verified both skill discovery and registered inspect_table invocation. Chinese PNG visually inspected; CSV/XLSX/Markdown generated with exact expected sample totals. No live model call.
Review: independent reviewer identified parser exception handling and Excel truncation; both corrected with regression coverage.
