---
name: mes-share
description: Share chosen dashboards of a my-easy-software project with other people with "mes expose" (Cloudflare Pages, Netlify or Vercel; only named people, anyone with the link, or public), or take a shared site down with "mes expose --remove". Use when the user wants to show a dashboard to a friend, family, a coach or the public, or to stop sharing it.
---

# Share dashboards

Run `mes guide share` and follow it. Always show the user the confirmation question mes returns
(which dashboards and data become visible, and to whom) and let them answer it; never answer it
for them. Start with `--dry-run`.

To stop sharing, do not just share again with fewer people or a new link: earlier versions stay
online until they are deleted. Use `mes expose <project> --to <provider> --remove` (it asks first),
and tell the user about any version mes says it could not delete.
