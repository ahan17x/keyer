#!/usr/bin/env python3
"""Summarise a GitHub `gds` workflow run for docs/AREA.md.

    python3 scripts/gds_report.py RUN_ID [--dir DIR] [--repo OWNER/REPO]

Downloads the run's GDS_logs and precheck_reports artifacts into DIR (default:
a temporary directory; GDS_logs is about 300 MB), or reuses them if DIR already
holds them, and prints: job conclusions and durations, cell and area numbers,
utilisation, setup and hold slack per corner, routing and other long steps,
DRC / LVS / antenna counts, the precheck table, and the worst setup violators
of any failing corner. Needs the GitHub CLI (`gh`).
"""

import glob
import json
import os
import subprocess
import sys
import tempfile


def gh(*args):
    return subprocess.run(["gh"] + list(args), capture_output=True, text=True, check=False).stdout


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    run_id, d, repo = argv[0], None, None
    for i, a in enumerate(argv):
        if a == "--dir":
            d = argv[i + 1]
        if a == "--repo":
            repo = argv[i + 1]
    rflag = ["-R", repo] if repo else []
    d = d or tempfile.mkdtemp(prefix="gds_report_")
    os.makedirs(d, exist_ok=True)
    info = json.loads(gh("run", "view", run_id, *rflag, "--json", "status,conclusion,headSha,headBranch,event,jobs") or "{}")
    print("Run %s: %s/%s on %s (%s), commit %s" % (run_id, info.get("status"), info.get("conclusion"),
                                                  info.get("headBranch"), info.get("event"), str(info.get("headSha"))[:7]))
    for j in info.get("jobs", []):
        print("  job %-9s %-10s %s -> %s" % (j["name"], j.get("conclusion") or j.get("status"), j.get("startedAt"), j.get("completedAt")))
    for art in ("GDS_logs", "precheck_reports"):
        if not os.path.isdir(os.path.join(d, art)):
            subprocess.run(["gh", "run", "download", run_id, *rflag, "-n", art, "-D", os.path.join(d, art)], check=False)
    mfiles = glob.glob(os.path.join(d, "GDS_logs", "runs", "*", "final", "metrics.json"))
    if not mfiles:
        print("no metrics.json (the gds job did not finish, or the artifact has expired)")
        return 1
    m = json.load(open(mfiles[0]))
    run_dir = os.path.dirname(os.path.dirname(mfiles[0]))

    def g(k, default="n/a"):
        return m.get(k, default)

    corners = sorted(k.split("corner:")[1] for k in m if k.startswith("timing__setup__ws__corner:"))
    print("\nCells: %s standard cells (%s sequential, %s timing-repair buffers, %s macros), %s instances with fill" % (
        g("design__instance__count__stdcell"), g("design__instance__count__class:sequential_cell"),
        g("design__instance__count__class:timing_repair_buffer"), g("design__instance__count__macros"),
        g("design__instance__count")))
    print("Area: stdcell %s um^2, macros %s um^2, core %s um^2; utilisation %.1f%% (stdcell %.1f%%)" % (
        g("design__instance__area__stdcell"), g("design__instance__area__macros"), g("design__core__area"),
        100 * float(g("design__instance__utilization", 0)), 100 * float(g("design__instance__utilization__stdcell", 0))))
    print("Timing (ns):")
    for c in corners:
        print("  %-22s setup %+8.3f (%s violations)   hold %+7.3f (%s violations)" % (
            c, m["timing__setup__ws__corner:" + c], g("timing__setup_vio__count__corner:" + c),
            m["timing__hold__ws__corner:" + c], g("timing__hold_vio__count__corner:" + c)))
    print("Routing: DRC errors %s (per iteration: %s); wire length %s um" % (
        g("route__drc_errors"), " ".join(str(m[k]) for k in sorted(m) if k.startswith("route__drc_errors__iter:")),
        g("route__wirelength")))
    print("Sign-off: Magic DRC %s, illegal overlaps %s, LVS errors %s, antenna nets %s pins %s; "
          "max slew %s, max cap %s, max fanout %s" % (
              g("magic__drc_error__count"), g("magic__illegal_overlap__count"), g("design__lvs_error__count"),
              g("antenna__violating__nets"), g("antenna__violating__pins"), g("design__max_slew_violation__count"),
              g("design__max_cap_violation__count"), g("design__max_fanout_violation__count")))
    print("Power %s W, worst IR drop %s V" % (g("power__total"), g("ir__drop__worst")))
    times = []
    for step in glob.glob(os.path.join(run_dir, "*", "runtime.txt")):
        times.append((open(step).read().split()[0], os.path.basename(os.path.dirname(step))))
    print("Longest steps: " + "; ".join("%s %s" % (n, t) for t, n in sorted(times, reverse=True)[:4]))
    pre = os.path.join(d, "precheck_reports", "results.md")
    if os.path.exists(pre):
        rows = [ln.strip() for ln in open(pre) if ln.startswith("|") and "| Result |" not in ln and "---" not in ln]
        print("Precheck: " + "; ".join(r.strip("|").replace("|", ":").strip() for r in rows))
    else:
        print("Precheck: no report artifact")
    for c in corners:
        if int(g("timing__setup_vio__count__corner:" + c, 0) or 0) > 0:
            for vl in glob.glob(os.path.join(run_dir, "*-openroad-stapostpnr", c, "violator_list.rpt")):
                lines = [ln.strip() for ln in open(vl) if ln.startswith("[setup")]
                print("Worst setup violators at %s:" % c)
                for ln in lines[:6]:
                    print("  " + ln)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
