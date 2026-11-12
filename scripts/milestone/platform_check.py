"""Indicator 6.1 measurements against a running ``luban serve`` instance.

Subcommands
-----------
``latency``  100 identical calls through the platform URL and directly, paired
             and alternating, reporting SR_call and the P50/P95/max added time.
``soak``     continuous load for at least an hour (>= 100 requests) while
             checking that the service process never restarts.
``faults``   malformed and hostile inputs; each must get a 4xx and the service
             must stay healthy afterwards.

Every subcommand writes ``<output>/<name>.json`` and ``<output>/<name>.md`` and
exits non-zero when its criterion is not met. Only the standard library is used.
"""

import argparse
import http.client
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

EPISODE_BODY = {"task": {"name": "increment"}, "max_steps": 3}


def call(
    url: str, path: str, body: Any, timeout: float = 30.0
) -> tuple[bool, float, int, Any]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
            status = response.status
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", "replace")
        status = error.code
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return False, time.perf_counter() - started, 0, str(error)
    elapsed = time.perf_counter() - started
    result = payload.get("result") if isinstance(payload, dict) else None
    ok = status == 200 and isinstance(result, dict) and result.get("success") is True
    return ok, elapsed, status, payload


def health(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            url.rstrip("/") + "/health", timeout=10
        ) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return None


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    rank = max(0, math.ceil(q / 100 * len(ordered)) - 1)
    return ordered[rank]


def write(output: Path, name: str, report: dict[str, Any], lines: list[str]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{name}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False)
    )
    (output / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def latency(args: argparse.Namespace) -> bool:
    rows = []
    path = "/v1/runs" if args.config else "/v1/episodes/run"
    body = {"config": args.config} if args.config else EPISODE_BODY
    for index in range(args.calls):
        order = (
            (("platform", "direct") if index % 2 == 0 else ("direct", "platform"))
            if args.direct_url
            else ("platform",)
        )
        row: dict[str, Any] = {"index": index}
        for side in order:
            url = args.platform_url if side == "platform" else args.direct_url
            ok, elapsed, status, payload = call(url, path, body, args.timeout)
            row[side] = {"ok": ok, "seconds": elapsed, "status": status}
            if not args.direct_url:
                server_seconds = None
                if isinstance(payload, dict):
                    if args.config:
                        result = payload.get("result")
                        if isinstance(result, dict):
                            server_seconds = result.get("service_function_seconds")
                    else:
                        server_seconds = payload.get("server_seconds")
                row["direct"] = {
                    "ok": ok and isinstance(server_seconds, (int, float)),
                    "seconds": server_seconds,
                    "status": status,
                }
        rows.append(row)
        print(
            f"[调用 {index + 1}/{args.calls}] {'成功' if row['platform']['ok'] else '失败'}",
            flush=True,
        )
    platform_ok = sum(row["platform"]["ok"] for row in rows)
    paired = [
        row["platform"]["seconds"] - row["direct"]["seconds"]
        for row in rows
        if row["platform"]["ok"] and row["direct"]["ok"]
    ]
    direct = [row["direct"]["seconds"] for row in rows if row["direct"]["ok"]]
    sr_call = platform_ok / args.calls
    p95 = percentile(paired, 95) if paired else float("inf")
    passed = sr_call >= 0.95 and p95 <= 1.0 and args.calls >= 100
    report = {
        "request_path": path,
        "config": args.config,
        "platform_url": args.platform_url,
        "direct_measurement": "paired_http_call"
        if args.direct_url
        else "registered_function_seconds"
        if args.config
        else "same_request_server_handler_seconds",
        "calls": args.calls,
        "platform_ok": platform_ok,
        "sr_call": sr_call,
        "direct_mean_seconds": statistics.fmean(direct) if direct else None,
        "added_seconds": {
            "p50": percentile(paired, 50) if paired else None,
            "p95": p95 if paired else None,
            "max": max(paired) if paired else None,
        },
        "passed": passed,
        "rows": rows,
    }
    lines = [
        "# 指标6.1 平台接入调用与耗时",
        "",
        f"- 正常调用：{platform_ok}/{args.calls}，SR_call = {sr_call:.2%}（要求 ≥ 95%）",
        f"- 功能执行基准平均耗时 t_direct = {report['direct_mean_seconds']}",
        f"- 基准计时口径：{report['direct_measurement']}",
        f"- 平台接入增加耗时 P50 = {report['added_seconds']['p50']} s，"
        f"P95 = {report['added_seconds']['p95']} s（要求 ≤ 1 s），"
        f"Max = {report['added_seconds']['max']} s",
        f"- 判定：{'合格' if passed else '不合格'}",
    ]
    write(args.output, "latency", report, lines)
    return passed


def soak(args: argparse.Namespace) -> bool:
    first = health(args.url)
    if first is None:
        raise SystemExit(f"service at {args.url} is not reachable")
    started = time.monotonic()
    rows = []
    restarts = 0
    last_uptime = first["uptime_seconds"]
    samples = []
    path = "/v1/runs" if args.config else "/v1/episodes/run"
    body = {"config": args.config} if args.config else EPISODE_BODY
    args.output.mkdir(parents=True, exist_ok=True)
    while time.monotonic() - started < args.seconds or len(rows) < args.min_requests:
        ok, elapsed, status, payload = call(args.url, path, body, args.timeout)
        rows.append(
            {
                "t": time.monotonic() - started,
                "ok": ok,
                "seconds": elapsed,
                "status": status,
                "request_id": payload.get("request_id")
                if isinstance(payload, dict)
                else None,
                "trace_path": payload.get("result", {}).get("trace_path")
                if isinstance(payload, dict) and isinstance(payload.get("result"), dict)
                else None,
            }
        )
        with (args.output / "soak_requests.jsonl").open("a") as stream:
            stream.write(json.dumps(rows[-1]) + "\n")
        state = health(args.url)
        if state is None or state["uptime_seconds"] < last_uptime:
            restarts += 1
        if state is not None:
            last_uptime = state["uptime_seconds"]
        if len(rows) % args.sample_every == 0:
            samples.append(
                {
                    "t": rows[-1]["t"],
                    "ok": sum(r["ok"] for r in rows),
                    "n": len(rows),
                    "health": state,
                }
            )
            print(
                f"[连续运行] {len(rows)} 次请求，成功 {sum(r['ok'] for r in rows)} 次，已运行 {rows[-1]['t']:.1f} 秒",
                flush=True,
            )
        time.sleep(args.interval)
    duration = time.monotonic() - started
    ok_count = sum(row["ok"] for row in rows)
    rate = ok_count / len(rows)
    passed = duration >= 3600 and len(rows) >= 100 and restarts == 0 and rate >= 0.95
    report = {
        "request_path": path,
        "config": args.config,
        "url": args.url,
        "duration_seconds": duration,
        "requests": len(rows),
        "ok": ok_count,
        "success_rate": rate,
        "restarts_or_unreachable": restarts,
        "passed": passed,
        "samples": samples,
        "rows": rows,
    }
    lines = [
        "# 指标6.1 连续运行稳定性",
        "",
        f"- 连续运行时长：{duration / 3600:.2f} h（要求 ≥ 1 h）",
        f"- 总请求数：{len(rows)}（要求 ≥ 100），成功 {ok_count}，成功率 {rate:.2%}（要求 ≥ 95%）",
        f"- 进程重启/不可达次数：{restarts}（要求 0）",
        f"- 判定：{'合格' if passed else '不合格'}",
    ]
    write(args.output, "soak", report, lines)
    return passed


FAULTS: list[tuple[str, str, Any]] = [
    ("空请求体", "/v1/episodes/run", b""),
    ("非 JSON", "/v1/episodes/run", b"{not json"),
    ("非对象 JSON", "/v1/episodes/run", [1, 2, 3]),
    ("未知字段", "/v1/episodes/run", {"task": {"name": "x"}, "shell": "rm -rf /"}),
    ("参数类型错误", "/v1/episodes/run", {"max_steps": "3"}),
    ("超范围参数", "/v1/episodes/run", {"max_steps": 10**9}),
    ("布尔冒充整数", "/v1/episodes/run", {"max_steps": True}),
    ("任务名非字符串", "/v1/episodes/run", {"task": {"name": ["a"]}}),
    (
        "超长字符串参数",
        "/v1/episodes/run",
        {"task": {"name": "a" * 200_000}, "max_steps": 1},
    ),
    ("超大请求体", "/v1/episodes/run", b"{" + b" " * 1_100_000 + b"}"),
    ("畸形任务图", "/v1/task-graph/validate", {"graph": {"nodes": "x"}}),
    (
        "任务图环路",
        "/v1/task-graph/validate",
        {
            "graph": {
                "nodes": [
                    {
                        "id": "a",
                        "instruction": "a",
                        "dependencies": ["b"],
                        "expected_outcome": "a",
                    },
                    {
                        "id": "b",
                        "instruction": "b",
                        "dependencies": ["a"],
                        "expected_outcome": "b",
                    },
                ]
            }
        },
    ),
    (
        "非法 max_nodes",
        "/v1/task-graph/validate",
        {"graph": {"nodes": []}, "max_nodes": -1},
    ),
    ("未知路径", "/v1/../../etc/passwd", {}),
]


def faults(args: argparse.Namespace) -> bool:
    results = []
    for name, path, body in FAULTS:
        if name == "超大请求体":
            # The service rejects Content-Length before reading the body. Wait
            # for that rejection instead of writing into an already closed socket.
            address = urlsplit(args.url)
            connection_type = (
                http.client.HTTPSConnection
                if address.scheme == "https"
                else http.client.HTTPConnection
            )
            connection = connection_type(
                address.hostname, address.port, timeout=args.timeout
            )
            started = time.perf_counter()
            try:
                connection.putrequest("POST", address.path.rstrip("/") + path)
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.endheaders()
                response = connection.getresponse()
                status = response.status
                response.read()
                elapsed = time.perf_counter() - started
            finally:
                connection.close()
        else:
            ok, elapsed, status, payload = call(args.url, path, body)
        alive = health(args.url) is not None
        expected = 400 <= status < 500 or (name == "超长字符串参数" and status == 200)
        results.append(
            {
                "case": name,
                "status": status,
                "alive_after": alive,
                "passed": expected and alive,
                "seconds": elapsed,
            }
        )
    if args.config:
        for body in [
            {"config": "unregistered"},
            {"config": args.config, "task": {}},
            {"config": 3},
        ]:
            _, elapsed, status, _ = call(args.url, "/v1/runs", body, args.timeout)
            alive = health(args.url) is not None
            results.append(
                {
                    "case": f"登记任务参数拒绝：{body}",
                    "status": status,
                    "alive_after": alive,
                    "passed": 400 <= status < 500 and alive,
                    "seconds": elapsed,
                }
            )
    ok, _, _, _ = call(
        args.url,
        "/v1/runs" if args.config else "/v1/episodes/run",
        {"config": args.config} if args.config else EPISODE_BODY,
        args.timeout,
    )
    passed = all(row["passed"] for row in results) and ok
    lines = [
        "# 指标6.1 关键异常注入与安全测试",
        "",
        "| 用例 | HTTP 状态 | 之后服务存活 | 结果 |",
        "|---|---:|---|---|",
    ]
    lines += [
        f"| {r['case']} | {r['status']} | {r['alive_after']} | {'通过' if r['passed'] else '失败'} |"
        for r in results
    ]
    lines += [
        "",
        f"- 注入后正常调用：{'成功' if ok else '失败'}",
        f"- 判定：{'全部通过' if passed else '未全部通过'}",
    ]
    write(
        args.output,
        "faults",
        {"cases": results, "normal_call_after": ok, "passed": passed},
        lines,
    )
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    lat = sub.add_parser("latency")
    lat.add_argument("--platform-url", required=True)
    lat.add_argument(
        "--direct-url",
        help="Optional paired direct endpoint; otherwise use server handler timing",
    )
    lat.add_argument("--calls", type=int, default=100)
    for name in ("soak", "faults"):
        sp = sub.add_parser(name)
        sp.add_argument("--url", required=True)
    sub.choices["soak"].add_argument("--seconds", type=float, default=3700)
    sub.choices["soak"].add_argument("--min-requests", type=int, default=100)
    sub.choices["soak"].add_argument("--interval", type=float, default=5.0)
    sub.choices["soak"].add_argument("--sample-every", type=int, default=60)
    for sp in sub.choices.values():
        sp.add_argument(
            "--config", help="Registered run name for real configured tasks"
        )
        sp.add_argument("--timeout", type=float, default=60)
        sp.add_argument("--output", type=Path, default=Path("runs/milestone_6_1"))
    args = parser.parse_args()
    passed = {"latency": latency, "soak": soak, "faults": faults}[args.command](args)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
