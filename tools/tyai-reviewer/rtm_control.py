"""Local operator inspection and explicit recovery; never an MCP client tool."""
import argparse
import json
from pathlib import Path

from rtm_queue import ResourceQueue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "recover"])
    parser.add_argument("--state", type=Path, default=Path(__file__).parent / ".state")
    parser.add_argument("--confirmed-safe", help="Recorded evidence that hardware is safe to hand over")
    args = parser.parse_args()
    queue = ResourceQueue(args.state / "rtm-resource.sqlite3")
    if args.command == "recover":
        if not args.confirmed_safe or not args.confirmed_safe.strip():
            parser.error("Recovery requires --confirmed-safe with actual verification evidence")
        print(json.dumps(queue.recover(args.confirmed_safe), ensure_ascii=False, indent=2))
        return
    with queue._transaction() as conn:
        resource = dict(conn.execute("SELECT state, reason FROM rtm_resource").fetchone())
        requests = [dict(row) for row in conn.execute(
            "SELECT s.session_id, r.state, s.pid, s.deadline FROM rtm_requests r "
            "JOIN rtm_sessions s ON s.owner = r.owner "
            "WHERE r.state IN ('WAITING', 'ACQUIRED', 'QUARANTINED') ORDER BY r.sequence")]
    print(json.dumps({"resource": resource, "requests": requests}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
