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
- **`merge` and `diff` compare a fabric against an intended configuration**,
  reporting both what the configuration asks for and the fabric lacks, and what
  the fabric carries and the configuration never mentions.

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
a4i daemon status                                 # is a token held, and for how long
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

```sh
a4i merge ./configs/ -o merged.json               # every *.json, in path order
a4i merge ./configs/ | a4i post mo uni --dry-run  # what posting it would change
a4i merge ./configs/ | a4i post mo uni
a4i merge ./configs/ | a4i diff
a4i merge ./configs/ | a4i diff --exclude uni/tn-common --exclude uni/infra
a4i post mo uni/tn-demo --dry-run '{"fvTenant":{"attributes":{"descr":"prod"}}}'
```

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

`post --dry-run` reads the same way over a single POST: it fetches the subtree
the body targets, prints what would change, and sends nothing. Both commands say
in their exit code whether anything would change:

| Code | Meaning |
| --- | --- |
| `0` | the fabric matches / posting this body would change nothing |
| `2` | it differs / the body would change something |
| `1` | the command itself failed (not logged in, bad JSON, unknown DN) |

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
| `dry_run` | what a POST would change, sending nothing |
| `post` | POST a body |
| `merge` | several bodies or paths folded into one |
| `diff` | the fabric compared against one configuration |

| Resource | Contents |
| --- | --- |
| `a4i://guide/post-body` | how an ACI body nests, how a child MO gets its DN, what `status` does |
| `a4i://guide/query` | class against MO queries, the two subtree controls, keeping a response small |
| `a4i://guide/workflow` | the order: search or list, describe, get, dry run, post |
| `a4i://guide/limits` | where the bundled model, the dry run and the diff each stop short |

A `get` whose response would exceed 64 KB is refused, with the total count and
the ways to narrow it; `A4I_MCP_MAX_BYTES` raises or lowers that. On a
`--read-only` session, `post` is not offered at all.

## Python library

A `Client` holds its own session, so no daemon is involved: it logs in itself
and keeps the token in memory for as long as it lives.

```python
import a4i
from a4i.merge import merge

with a4i.Client("apic1.example.com", verify=False) as client:
    client.login("admin", password)

    data = client.get("fvTenant", kind="class", query_target="subtree", rsp_subtree="full")
    client.post("uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo")
    changes = client.diff(merge(base, override))
```

`kind` is the subcommand the CLI takes, and it is required: `"class"` for a
class name, `"mo"` for a DN. Every other keyword argument is the CLI option of
the same name with underscores. `verify` is what `-k/--insecure` and `--ca`
express, `timeout` is `login --timeout`, and `dry_run()`, `diff()` and
`a4i.merge.merge()` are the commands of those names.

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
