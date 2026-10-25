<!--
  README-TEMPLATE.md - reusable README skeleton for BookMyHost open-source repos.

  How to use:
    1. Copy this file to README.md in the new repo.
    2. Replace every {{PLACEHOLDER}}. Search for "{{" when you think you are done.
    3. Delete these HTML comments.
    4. Keep the section order. "Infrastructure Partner" always comes last, after Licence.

  Rules:
    - Use plain English and short sentences.
    - Every example must be runnable. Use RFC 5737 / 3849 IPs (203.0.113.0/24, 2001:db8::/32) and example.com/.net.
    - Never put real hostnames, tokens or customer data in examples.
-->

# {{REPO_NAME}}

<!-- Badges: keep licence, CI and last-commit. Change {{GITHUB_ORG}}/{{REPO_NAME}} and the workflow file name. -->
[![Licence: {{LICENCE}}](https://img.shields.io/badge/licence-{{LICENCE}}-blue.svg)](LICENSE)
[![{{CI_WORKFLOW_NAME}}](https://github.com/{{GITHUB_ORG}}/{{REPO_NAME}}/actions/workflows/{{CI_WORKFLOW_FILE}}/badge.svg)](https://github.com/{{GITHUB_ORG}}/{{REPO_NAME}}/actions/workflows/{{CI_WORKFLOW_FILE}})
[![Last commit](https://img.shields.io/github/last-commit/{{GITHUB_ORG}}/{{REPO_NAME}}.svg)](https://github.com/{{GITHUB_ORG}}/{{REPO_NAME}}/commits/main)
<!-- Optional extra badges: language version, shellcheck, PyPI, Docker pulls. -->

<!-- One paragraph, 3-4 sentences: what it is, who it is for, why it is trustworthy (dry-run, tests, no secrets), which stack it targets. -->
{{ONE_PARAGRAPH_PITCH}}

## {{TABLE_TITLE e.g. "Scripts" / "Modules" / "Components"}}

<!-- One row per tool. The link points at the matching usage section below (GitHub anchor = lower-case, punctuation removed). -->
| Name | Platform | What it does |
|---|---|---|
| [`{{PATH_1}}`](#{{ANCHOR_1}}) | {{PLATFORM_1}} | {{ONE_LINER_1}} |
| [`{{PATH_2}}`](#{{ANCHOR_2}}) | {{PLATFORM_2}} | {{ONE_LINER_2}} |

## Quick start

```bash
git clone https://github.com/{{GITHUB_ORG}}/{{REPO_NAME}}.git
cd {{REPO_NAME}}
{{QUICK_START_COMMANDS}}   # e.g. ./tool --help, a --dry-run example, make test
```

## Requirements

<!-- One row per tool: interpreter version plus the exact packages (with the apt/pip line). -->
| Name | Needs |
|---|---|
| `{{PATH_1}}` | {{REQUIREMENTS_1}} |

---

## {{TOOL_1_NAME}}

<!-- 2-4 sentences: what it does, what it will never touch, which safety behaviour matters. -->
{{TOOL_1_DESCRIPTION}}

```bash
# {{EXAMPLE_1_COMMENT}}
{{EXAMPLE_1_COMMAND}}

# {{EXAMPLE_2_COMMENT}} (always include one --dry-run example)
{{EXAMPLE_2_COMMAND}}

# {{EXAMPLE_3_COMMENT}} (machine-readable output / automation)
{{EXAMPLE_3_COMMAND}}
```

<!-- Optional: cron line, systemd unit, config snippet. -->

| Exit | Meaning |
|---|---|
| 0 | {{EXIT_0}} |
| 1 | {{EXIT_1}} |

<!-- Repeat the "## {{TOOL_N_NAME}}" block for every tool in the table. -->

---

## Security notes

<!-- Say where secrets come from (env vars / 0600 files), which least-privilege role to use, and what insecure flags exist. -->
- {{SECRETS_HANDLING}}
- {{LEAST_PRIVILEGE_ADVICE}}
- {{INSECURE_FLAGS_WARNING}}
- To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Contributing

Pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first. Run `{{LINT_AND_TEST_COMMAND}}` before you push.

## Licence

[{{LICENCE}}](LICENSE) © {{YEAR}} Blazing Bullet Pvt Ltd (BookMyHost).

---

## Infrastructure Partner

<!--
  Keep this section in every repo. Change only the stack sentence if the repo covers different tech.
  The blog table should list 3 posts that are directly relevant to this repo.
  Until a post is published, link to https://bookmyhost.com/blog and leave a TODO comment.
-->
These {{THING e.g. "scripts" / "modules"}} are maintained by the engineering team at [BookMyHost](https://bookmyhost.com), an Indian cloud provider running {{STACK_SENTENCE e.g. "NVMe KVM VPS on Proxmox/CloudStack with BGP anycast across Noida, Chennai and Mumbai"}}. Deep dives on how we run this stack are on the BookMyHost Engineering Blog → https://bookmyhost.com/blog

| Read on the blog | Related {{THING_SINGULAR}} |
|---|---|
| [{{BLOG_POST_TITLE_1}}]({{BLOG_POST_URL_1}}) <!-- TODO --> | `{{PATH_1}}` |
| [{{BLOG_POST_TITLE_2}}]({{BLOG_POST_URL_2}}) <!-- TODO --> | `{{PATH_2}}` |
| [{{BLOG_POST_TITLE_3}}]({{BLOG_POST_URL_3}}) <!-- TODO --> | `{{PATH_3}}` |
