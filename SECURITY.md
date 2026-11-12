# Security Policy

M3U Proxy fetches streams on behalf of players, sits between your network and your providers, and is often reachable from the internet through M3U Editor. We take security reports seriously and treat them as our top priority.

## Reporting a vulnerability

**Please don't report security problems in public issues, discussions, or public Discord channels.**

Report them privately through GitHub:

1. Go to the [Security tab](https://github.com/m3ue/m3u-proxy/security) of this repository.
2. Click **Report a vulnerability**.
3. Fill in the form. Only the maintainers can see it.

Or send a direct message to **shparkison** on [Discord](https://discord.gg/rS3abJ5dz7). Please don't post it in a channel, even to ask who to talk to.

Please include:

- what the problem is and what an attacker could do with it,
- the M3U Proxy version you tested, and whether it was standalone or embedded in M3U Editor,
- steps to reproduce, or a proof of concept,
- whether it needs the API token or a signed URL.

## What happens next

- We'll acknowledge your report within **3 days**.
- We'll confirm whether it's a vulnerability, and keep you updated while we work on a fix.
- Serious problems get a fix and a release as quickly as we can, usually within days, not weeks.
- Once a fixed release is out, we publish a [GitHub security advisory](https://github.com/m3ue/m3u-proxy/security/advisories) and explain what to update and why in the release notes and on the [blog](https://m3ue.sparkison.dev/blog).
- We'll credit you in the advisory, unless you'd rather we didn't.

Please give us a reasonable chance to release a fix before you disclose the problem publicly.

## Supported versions

Security fixes go into the next release, and into the next M3U Editor release for the embedded proxy. We don't backport fixes to older versions, so please keep up to date.

| Version | Supported |
| ------- | --------- |
| Latest release | ✅ |
| Older releases | ❌ |

## Scope

In scope:

- M3U Proxy itself, including its stream, segment, transcoding, and management API routes
- the official Docker images
- anything that lets a request read files, reach other addresses on your network, or use the proxy without the API token or a valid signed URL

Out of scope:

- running without an `API_TOKEN`, or with a short or guessable one, which the docs warn against
- vulnerabilities in your IPTV provider or upstream streams
- vulnerabilities in FFmpeg or other upstream dependencies that are already public, unless the proxy makes them easier to exploit
- denial of service by flooding requests or opening many streams

Not sure if something is in scope? Report it anyway, and we'll work it out together.
