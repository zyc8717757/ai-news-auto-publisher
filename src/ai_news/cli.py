from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .ai import AIUnavailable, Enhancer
from .collector import Collector
from .config import load_config
from .mailer import MailConfig, deliver
from .models import Digest, IssueState, NewsEvent, SourceItem
from .network import PublicHTTP
from .pipeline import cutoff_for, enhance_frozen, generate, published_digests, scheduled_issue
from .site import audit_site, build_site, digest_url
from .store import Store, atomic_write


def check_base(url: str) -> None:
    if "example.github.io" in url or "example.com" in url:
        raise ValueError("Configure the actual Pages base_url before deployment or email")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="AI News Free Edition — manual X publishing only")
    root.add_argument("--config", type=Path, default=Path("config"))
    root.add_argument("--data", type=Path, default=Path(".local/data"))
    root.add_argument("--remote", action="store_true", help="Push public state through a news-data checkout")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config")
    upgrade = sub.add_parser("enhance-issue")
    upgrade.add_argument("--issue", required=True)
    upgrade.add_argument("--result", type=Path, default=Path(".runtime/result.json"))
    upgrade.add_argument(
        "--test-extra-requests",
        type=int,
        choices=[0, 1, 2, 3, 4, 5],
        default=0,
        help="Explicit cumulative test ceiling; all historical counters remain",
    )
    ai_check = sub.add_parser("check-ai")
    ai_check.add_argument(
        "--issue", required=True, help="Check frozen public evidence without deploying or emailing"
    )
    sources = sub.add_parser("check-sources")
    sources.add_argument("--report", type=Path, default=Path(".local/source-health.json"))
    sources.add_argument(
        "--enforce", action="store_true", help="Require 20 healthy sources, including 8 primary"
    )
    preview = sub.add_parser("preview")
    preview.add_argument("--fixture", type=Path, default=Path("examples/demo-digest.json"))
    preview.add_argument("--output", type=Path, default=Path(".local/preview"))
    gen = sub.add_parser("generate")
    gen.add_argument("--issue")
    gen.add_argument("--schedule", help="Cron matching one of the configured Beijing report times")
    gen.add_argument("--regenerate", action="store_true")
    gen.add_argument("--result", type=Path, default=Path(".runtime/result.json"))
    site = sub.add_parser("build-site")
    site.add_argument("--output", type=Path, default=Path("site"))
    site.add_argument("--allow-sample-url", action="store_true")
    site.add_argument("--issue", help="Publish only this pending issue plus previously published revisions")
    audit = sub.add_parser("audit-site")
    audit.add_argument("--output", type=Path, default=Path("site"))
    deploy = sub.add_parser("mark-deployment")
    deploy.add_argument("--issue", required=True)
    deploy.add_argument("--failed", action="store_true")
    mail = sub.add_parser("send")
    mail.add_argument("--issue", required=True)
    mail.add_argument("--resend", action="store_true", help="Explicit resend, including uncertain deliveries")
    return root


def execute(args) -> int:
    settings, sources = load_config(args.config)
    if os.environ.get("PUBLISHER_PAUSED") == "true":
        settings.paused = True
    for variable, field in (("AI_ENABLED", "ai_enabled"), ("EMAIL_ENABLED", "email_enabled")):
        if variable in os.environ:
            setattr(settings, field, os.environ[variable] == "true")
    if os.environ.get("PAGES_BASE_URL"):
        settings.base_url = os.environ["PAGES_BASE_URL"]
    if args.command == "check-config":
        print(
            json.dumps(
                {
                    "status": "ok",
                    "sources": len(sources),
                    "ai_enabled": settings.ai_enabled,
                    "email_enabled": settings.email_enabled,
                    "paused": settings.paused,
                }
            )
        )
        return 0
    if args.command == "check-sources":
        enabled = [s for s in sources if s.enabled]
        items, caches = asyncio.run(Collector(settings).collect(enabled, {}, datetime.now(UTC)))
        report = [
            {
                "id": s.id,
                "trust": s.trust,
                "status": caches[s.id].status,
                "error_code": caches[s.id].error_code,
                "items": caches[s.id].verified
                if s.kind == "discovery"
                else sum(i.source_id == s.id for i in items),
            }
            for s in enabled
        ]
        atomic_write(args.report, json.dumps(report, indent=2))
        healthy = sum(r["status"] == "ok" for r in report)
        primary = sum(r["status"] == "ok" and r["trust"] in {"official", "research"} for r in report)
        print(
            json.dumps({"healthy": healthy, "primary": primary, "configured": len(report), "sources": report})
        )
        return 1 if args.enforce and (healthy < 20 or primary < 8) else 0
    if args.command == "preview":
        # Preview never invokes Gemini, sends mail, or touches persistent/remote state.
        digest = Digest.model_validate_json(args.fixture.read_text("utf-8"))
        build_site([digest], args.output, settings.base_url, os.environ.get("GITHUB_REPOSITORY", ""))
        print(json.dumps({"status": "preview_ready", "mode": digest.mode}))
        return 0
    if args.command == "audit-site":
        audit_site(args.output)
        print('{"status":"public_artifact_valid"}')
        return 0
    if settings.paused:
        print('{"status":"paused"}')
        return 0
    store = Store(args.data, remote=args.remote)
    if args.command == "enhance-issue":
        cutoff_for(args.issue)
        digest, created = enhance_frozen(
            settings, store, args.issue, test_extra_limit=args.test_extra_requests
        )
        result = {
            "status": "ready",
            "issue": digest.issue,
            "key": digest.key,
            "mode": digest.mode,
            "created": created,
            "count": len(digest.stories),
        }
        atomic_write(args.result, json.dumps(result))
        print(json.dumps(result))
        return 0
    if args.command == "check-ai":
        cutoff_for(args.issue)
        digest = store.latest(args.issue)
        if not digest or not digest.stories:
            raise ValueError("A nonempty frozen digest is required")
        events = [
            NewsEvent(
                id=s.id,
                evidence=[
                    SourceItem(**ref.model_dump(), collected_at=datetime.now(UTC), excerpt=s.summary[:1600])
                    for ref in s.sources
                ],
            )
            for s in digest.stories
        ]
        enhancer = Enhancer(settings, store, issue=args.issue)
        try:
            response = enhancer.enhance(events)
        except AIUnavailable as exc:
            print(json.dumps({"status": "ai_check_failed", "reason": str(exc), "model": enhancer.used_model}))
            return 1
        print(
            json.dumps(
                {"status": "ai_check_passed", "model": enhancer.used_model, "count": len(response.stories)}
            )
        )
        return 0
    if args.command == "generate":
        issue = args.issue or (
            scheduled_issue(args.schedule, datetime.now(UTC), settings.morning_time, settings.evening_time)
            if args.schedule
            else None
        )
        if not issue:
            atomic_write(args.result, '{"status":"skipped"}')
            print('{"status":"skipped"}')
            return 0
        digest, created = generate(settings, sources, store, issue, args.regenerate)
        result = {
            "status": "ready",
            "issue": digest.issue,
            "key": digest.key,
            "mode": digest.mode,
            "created": created,
            "count": len(digest.stories),
        }
        atomic_write(args.result, json.dumps(result))
        print(json.dumps(result))
    elif args.command == "build-site":
        if not args.allow_sample_url:
            check_base(settings.base_url)
        digests = store.digests()
        if args.issue:
            cutoff_for(args.issue)
            target = store.latest(args.issue)
            if target is None:
                raise ValueError("Missing publication target")
            digests = list({d.key: d for d in [*published_digests(store), target]}.values())
        build_site(digests, args.output, settings.base_url, os.environ.get("GITHUB_REPOSITORY", ""))
        print('{"status":"site_ready"}')
    elif args.command in {"mark-deployment", "send"}:
        cutoff_for(args.issue)
        digest = store.latest(args.issue)
        if not digest:
            raise ValueError("No frozen digest for this issue")
        state = store.state.issues.setdefault(digest.key, IssueState())
        check_base(settings.base_url)
        if args.command == "mark-deployment":
            if args.failed:
                state.deployment = "failed"
            else:

                async def verify():
                    page = await PublicHTTP().get(digest_url(settings.base_url, digest))
                    # A stale deployment can return 200; require this exact frozen revision timestamp.
                    if digest.label not in page.text or f'data-digest="{digest.key}"' not in page.text:
                        raise RuntimeError("deployed_digest_not_confirmed")

                asyncio.run(verify())
                state.deployment = "ready"
            store.save_state()
            print(json.dumps({"status": state.deployment}))
        elif not settings.email_enabled:
            if state.email == "pending":
                state.email = "disabled"
                store.save_state()
            print('{"status":"email_disabled"}')
        else:
            result = deliver(digest, store, MailConfig.from_env(), settings.base_url, args.resend)
            print(json.dumps({"status": result}))
            return 0 if result in {"sent", "already_sent"} else 1
    return 0


def main() -> int:
    args = parser().parse_args()
    try:
        return execute(args)
    except AIUnavailable as exc:
        print(
            json.dumps({"status": "failed", "error_type": "AIUnavailable", "reason": str(exc)}),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        # Public logs receive only exception type, never private content/config or HTTP response text.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
