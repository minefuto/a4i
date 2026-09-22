# aciapi(a4i)

[![test](https://github.com/minefuto/a4i/actions/workflows/test.yml/badge.svg)](https://github.com/minefuto/a4i/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/a4i.svg)](https://pypi.org/project/a4i/)

CLI/MCP/Python Library for the Cisco ACI REST API.

- **The token is never written to disk.** `login` hands it to a small per-user
  daemon that holds it in memory, behind a Unix domain socket, so it survives
  across short-lived CLI invocations without touching the filesystem.
- **The ACI object model ships with it.** `search` and `describe` answer what a
  class is called and what a body may set on it, without an APIC and without a
  login.
- **`fetch` reads the fabric once, and the daemon holds it.** `diff` and `plan`
  compare against what it read and send nothing of their own, so a fabric read
  once answers any number of questions, and `post --dry-run` uses it too when it
  is there. A POST drops it, because it is no longer what the fabric holds.
- **`merge` and `diff` compare a fabric against an intended configuration**,
  reporting both what the configuration asks for and the fabric lacks, and what
  the fabric carries and the configuration never mentions.
- **`plan` posts only what changes.** It narrows a configuration to the MOs a
  POST of it would actually change, so the MOs the fabric already agrees with
  are never handed back to the APIC to be written again.

## Install

```sh
pip install a4i
```

Shell completion is printed to standard output; add one line to your shell's
startup file:

```sh
eval "$(a4i generate-shell-completion zsh)"    # ~/.zshrc, after compinit
eval "$(a4i generate-shell-completion bash)"   # ~/.bashrc
a4i generate-shell-completion fish | source    # ~/.config/fish/config.fish
```

## Usage

```sh
a4i login apic1.example.com -u admin              # prompts for password; -k if self-signed
a4i get class fvTenant                            # class query
a4i get mo uni/tn-common                          # MO query, by DN
a4i get class fvTenant --query-target subtree --rsp-subtree full
a4i get class l1PhysIf --node leaf101.example.com # query a switch with the same token
echo '{"fvTenant":{"attributes":{"name":"demo"}}}' | a4i post mo uni/tn-demo
a4i logout                                        # drop the in-memory session
a4i daemon status                                 # what is held: a token, and a fabric
a4i login apic1.example.com -u admin --read-only  # a session that will refuse every POST
a4i mcp                                           # serve MCP on stdio for an LLM client
```

`get`, `post` and `list` each take a `class` or an `mo` subcommand, so a DN
needs no leading `/` and a class name is never mistaken for one. Every `get`
option is named after the ACI query parameter it sets, so a parameter read in
the APIC REST API documentation can be typed as-is -- `a4i get class --help`
lists them.

### Reading the model

```sh
a4i search 'bridge domain'          # which class is that, by name
a4i describe fvBD                   # what a body may set on it
a4i list class fvT                  # class names starting with fvT
a4i list mo uni/tn-common           # the MOs one level under that DN
```

`search`, `describe` and `list class` read the bundled dictionary, so they need
neither a login nor a daemon. `list mo` asks the APIC for one level of children,
so it needs a session.

```
$ a4i describe fvCtx
fvCtx  VRF
The private layer 3 network context that belongs to a specific tenant or is
shared.

rn  ctx-{name}
dn  uni/tn-{name}/ctx-{name}
in  fvTenant

properties (13 settable, 14 read-only hidden)
  descr                string:Basic (0-128)            Specifies a descriptio…
  ipDataPlaneLearning  disabled|enabled = enabled
  name*                string:Basic (1-64)             A name for the network…
  pcEnfDir             egress|ingress|mixed = ingress  Policy Control Enforce…
  pcEnfPref            enforced|unenforced = enforced
  …
children (42)  --children to list them
```

A `*` marks a naming property -- the one the RN is built from. The middle column
is what the property accepts, with the default after `=`. `-a` spells out the
read-only properties, `--children` the classes that may hang under this one, and
`--json` prints the underlying record instead of the layout.

### Comparing a configuration

`merge` folds a configuration written across several files into the one body
that `diff` and `post` each take, later files winning attribute by attribute.
Two files mean the same MO when they resolve to the same DN, however each one
said so. The result is a `polUni` holding every merged MO nested under the MO it
hangs off, which is the shape a POST to `uni` takes; `diff` compares that same
body against everything the fabric has under `uni`.

`fetch` is what reads the fabric. It walks everything under `uni` and leaves it
in the daemon, where `diff` and `plan` take it from: they send nothing to the
APIC themselves, so one `fetch` serves as many comparisons as you like. It
prints what it read and nothing else -- the body is not output, because nothing
downstream needs it in a file.

```sh
a4i merge ./configs/ -o merged.json               # every *.json, in path order
a4i fetch                                         # read the fabric, once
a4i diff merged.json
a4i diff merged.json --exclude uni/tn-common --exclude uni/infra
a4i post mo uni/tn-demo --dry-run '{"fvTenant":{"attributes":{"descr":"prod"}}}'
```

A POST drops what `fetch` read: it is no longer what the fabric holds. So do a
login, a logout and a session expiry. `diff` and `plan` say so and stop rather
than comparing against a fabric nobody read (`post --dry-run` does not -- it
reads what the body names instead, and says so):

```
$ a4i post mo uni plan.json && a4i diff merged.json
error: no fabric has been fetched: run 'a4i fetch' first
(the cache is dropped by a post, a login, a logout, and a session expiry)
```

`a4i daemon status` says what is held and how old it is.

`plan` narrows that same configuration to the MOs posting it would change, and
writes them out as a body of their own. Posting the whole configuration hands
the APIC every MO it already agrees with, and the APIC writes all of them;
posting a plan writes those MOs and nothing else. It prints no report: run
`post mo uni merged.json --dry-run` to read what changes, against the same
fetched fabric, and then plan and post it.

```sh
a4i fetch
a4i post mo uni merged.json --dry-run      # what would change
a4i merge ./configs/ | a4i plan            # the body, on stdout
a4i plan merged.json -o plan.json          # -o and --force, as merge takes them
a4i plan merged.json -o plan.json && a4i post mo uni plan.json
```

The body is shaped exactly as a merged one is, so the two can be read side by
side. An MO that changes carries the attributes that change and a `status`
saying what the fetched fabric was found to be -- so a fabric that has moved on
since refuses the POST rather than doing something nobody read. The only MOs it
can create are the ones it marks `status="created"`; the rest are there to nest
what does change under them, and carry `rn` and `status="modified"` only.

Every DN on the way down from `uni` has to be described by something: a BD
written without its tenant is refused rather than nested under a tenant `merge`
made up, and an MO whose DN does not sit under `uni` is refused too -- post that
one on its own. `--loose` fills the missing ancestor in instead, where the
bundled dictionary settles what class sits there.

```
- fvTenant uni/tn-common  (extra: 2 child MOs)
  - descr: ""
  - name: "common"

~ fvBD uni/tn-demo/BD-bd1
  ~ mtu: "1500" -> "9000"

+ fvTenant uni/tn-new  (missing: 2 child MOs)
  + descr: "added"
  + name: "new"

1 missing, 1 modified, 1 extra
```

`+` is an MO the configuration asks for and the fabric does not have, `-` one
the fabric has and the configuration does not mention, and `~` one whose
attributes differ. A wholly missing or wholly extra subtree is reported as its
top MO with the MOs below it counted; `--expand` lists every one of them.

The configuration is taken to describe the whole of `uni`, so everything it
leaves out is `extra` -- including `tn-common`, `tn-infra`, `tn-mgmt` and the
policies the APIC creates for itself. `--exclude` is how the rest is quietened:
it takes a DN, or a pattern whose `*` matches within one RN, and is repeatable.
A leading `!` makes one an exception to the others, so
`--exclude 'uni/tn-*' --exclude '!uni/tn-mgmt'` compares that tenant and no
other. A trailing `[key=value]` narrows one to the MOs whose attribute matches,
for what a DN cannot tell apart:
`--exclude 'uni/infra/accportprof-*/hports-*[descr=auto-*]'`.

`post --dry-run` asks the same question of a single POST, and takes whichever
fabric is cheaper. If `fetch` has left one in the daemon it compares against
that and sends nothing; if it has not, it reads the subtree the body targets --
one GET, where a fetch would have read the whole of `uni` to answer the same
question about one tenant. It says which of the two it did, and never stops for
want of a fetch the way `diff` and `plan` do. Both commands say in their exit
code whether anything would change:

| Code | Meaning |
| --- | --- |
| `0` | the fabric matches / posting this body would change nothing |
| `2` | it differs / the body would change something |
| `1` | the command itself failed (nothing fetched, not logged in, bad JSON) |

## MCP server

`a4i mcp` speaks the Model Context Protocol on stdin and stdout, so an LLM
client can read and write the fabric through the session you already logged in.
Register it with the client, then log in from a terminal as usual -- there is no
login tool, because this server never handles a password.

```json
{"mcpServers": {"a4i": {"command": "a4i", "args": ["mcp"]}}}
```

| Tool | What it does |
| --- | --- |
| `search` | find a class by what it is called |
| `describe` | one class from the bundled model, as a JSON record |
| `list` | class names by prefix, or the DNs one level under a DN |
| `get` | a class or MO query, with every query option under its own name |
| `dry_run` | what a POST would change, sending no POST |
| `post` | POST a body |
| `merge` | several bodies or paths folded into one |
| `fetch` | read the fabric into the session, for `diff` and `plan` |
| `plan` | one configuration narrowed to the MOs a POST would change |
| `diff` | the fabric compared against one configuration |

| Resource | Contents |
| --- | --- |
| `a4i://guide/post-body` | how an ACI body nests, how a child MO gets its DN, what `status` does |
| `a4i://guide/query` | class against MO queries, the two subtree controls, keeping a response small |
| `a4i://guide/workflow` | the two paths: changing one MO, and applying a whole configuration |
| `a4i://guide/limits` | where the bundled model, the dry run and the diff each stop short |

A `get` whose response would exceed 64 KB is refused, with the total count and
the ways to narrow it; `A4I_MCP_MAX_BYTES` raises or lowers that. On a
`--read-only` session, `post` is not offered at all.

## Python library

A `Client` holds its own session, so no daemon is involved: it logs in itself
and keeps the token in memory for as long as it lives.

```python
import a4i
from a4i.diff import compare
from a4i.dry_run import check
from a4i.merge import merge
from a4i.plan import create

with a4i.Client("apic1.example.com", verify=False) as client:
    client.login("admin", password)

    data = client.get("fvTenant", kind="class", query_target="subtree", rsp_subtree="full")
    client.post("uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo")

    fabric = client.fetch()
    changes = compare(merge(base, override), fabric=fabric)
    tenant = {"fvTenant": {"attributes": {"descr": "prod"}}}
    check("uni/tn-demo", tenant, kind="mo", fabric=fabric)  # or client.dry_run(...)
    client.post("uni", create(merge(base, override), fabric=fabric), kind="mo")
```

`kind` is the subcommand the CLI takes, and it is required: `"class"` for a
class name, `"mo"` for a DN. Every other keyword argument is the CLI option of
the same name with underscores. `verify` is what `-k/--insecure` and `--ca`
express, and `timeout` is `login --timeout`.

`fetch()` reads the whole of `uni` and returns it as one body; no daemon is
involved here, so it is yours to hold for as long as it is worth holding.
`a4i.diff.compare()`, `a4i.plan.create()` and `a4i.dry_run.check()` are `diff`,
`plan` and the dry run, and each takes that body as a keyword-only `fabric` --
they perform no I/O, so what they compared against is whatever you last read.
`client.dry_run()` is the one that reads for itself, fetching only the subtrees
the body names, which is what `post --dry-run` falls back to.

`AsyncClient` is `Client` awaited: the same arguments, the same return values
and the same exceptions, sending the same requests in the same order.

```python
async with a4i.AsyncClient("apic1.example.com", verify=False) as client:
    await client.login("admin", password)
    data = await client.get("fvTenant", kind="class")
```

A value ACI does not define raises `ValueError` before anything is sent. A
failed request raises `a4i.ApicError`, `a4i.NotLoggedInError` or
`a4i.SessionExpiredError`, all of them `a4i.A4iError`. The token refreshes
itself once half its lifetime has elapsed, so a long-running script needs
nothing of its own.
