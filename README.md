# aciapi(a4i)

CLI/Python Library for the Cisco ACI REST API.

`login` authenticates once; the token is held **only in memory** by a small
per-user daemon, never written to disk. Subsequent `get` / `post` / `list` /
`diff` commands talk to that daemon over a Unix domain socket, so the token
survives across short-lived CLI invocations without touching the filesystem.

What a target names is said outright rather than inferred: `get`, `post` and
`list` each take a `class` or an `mo` subcommand, so a DN needs no leading `/`
and a class name is never mistaken for one. `a4i list` is how you find either.

## Install

```sh
uv sync
```

### Shell completion

`a4i generate-shell-completion SHELL` prints the widget to standard output. Add
one line to your shell's startup file:

```sh
# ~/.zshrc -- after "autoload -Uz compinit && compinit", which defines compdef
eval "$(a4i generate-shell-completion zsh)"

# ~/.bashrc
eval "$(a4i generate-shell-completion bash)"

# ~/.config/fish/config.fish
a4i generate-shell-completion fish | source
```

That runs `a4i` once per shell startup. To avoid even that, write the zsh widget
to a file on your `fpath` instead -- it carries a `#compdef` line, so autoloading
it works too:

```sh
mkdir -p ~/.zfunc && a4i generate-shell-completion zsh > ~/.zfunc/_a4i
```

Completion comes from the CLI's own argument parser and nothing else: commands,
the `class` / `mo` subcommands, option names, and the values of options that have
a fixed set of them -- including after a comma, so
`--rsp-subtree-include faults,no<TAB>` works. It reaches neither the APIC nor the
daemon nor a dictionary, so a tab press is always answered at once.

Class names and DNs are not completed. They come from `a4i list` instead, where
the wait is one you asked for rather than one a tab press imposes.

## Usage

```sh
a4i login apic1.example.com -u admin              # prompts for password
a4i get class fvTenant                            # class query
a4i get mo uni/tn-common                          # MO query, by DN
a4i get mo uni                                    # MO query, top of the MO tree
a4i get class fvTenant --query-target subtree --rsp-subtree full
a4i get class l1PhysIf --node leaf101.example.com # query a switch with the same token
a4i list class fvT                                # which classes start with fvT
a4i list mo uni/tn-common                         # what hangs under that DN
echo '{"fvTenant":{"attributes":{"name":"demo"}}}' | a4i post mo uni/tn-demo
a4i diff ./configs/                               # compare the fabric with a configuration
a4i logout                                        # drop the in-memory session
a4i daemon status                                 # is a token held, and for how long
a4i login apic1.example.com -u admin --read-only  # a session that will refuse every POST
a4i mcp                                           # serve MCP on stdio for an LLM client
```

Self-signed APICs: add `-k/--insecure` (or `--ca path/to/ca.pem`) on `login`.

`get`, `post` and `diff` colorize their output on a terminal and print it plain
when piped or redirected. `--raw` forces the plain form on a terminal too; the
content is the same either way, so a script gets what it would have got from a
pipe. `list` prints plain names always, and has no `--raw` to force.

`a4i daemon status` says whether a token is held, for which user and host, and
how long it has left; `a4i daemon stop` ends the daemon, dropping the token with
it. Neither starts a daemon that is not already running, so both are safe to run
blind:

```
$ a4i daemon status
logged in to https://apic1.example.com as admin, expires in 512s
$ a4i daemon stop
daemon stopped
$ a4i daemon status
daemon not running
```

`logout` and `daemon stop` both end the session on the APIC and drop the token;
`logout` leaves the daemon running for the next login, while `daemon stop` ends
the process. If the APIC cannot be told, the token is still dropped here and the
command still succeeds, over a warning that only the APIC's own copy of the
session is left to expire on its own.

### A session that cannot write

`login --read-only` makes the daemon refuse every POST for as long as it lives.
It is the daemon that refuses, not the command, so it holds for everything
reaching that session -- `a4i post`, and the MCP server below, alike.

```
$ a4i login apic1.example.com -u admin --read-only
logged in to https://apic1.example.com as admin (read-only)
$ echo '{"fvTenant":{"attributes":{"name":"demo"}}}' | a4i post mo uni/tn-demo
error: this session is read-only (logged in with --read-only); run 'a4i daemon stop' and log in again to write
```

`post --dry-run` still works: it issues GETs and nothing else, so what a POST
would change can be worked out on a session that could not perform it.

Nothing clears the flag but ending the daemon -- not `logout`, and not another
`login` without the option. A flag a fresh login could drop would guarantee
nothing, since the next login is exactly what it has to hold against. `a4i
daemon status` says so, and a login that asked to write and did not get it says
so too.

### Finding a class name or a DN

`a4i list` answers the two questions a target raises, in the two places the
answers live.

```sh
a4i list class                      # every class name in the bundled dictionary
a4i list class fvT                  # only those starting with fvT
a4i list mo                         # the MOs directly under uni
a4i list mo uni/tn-common           # the MOs directly under that DN
a4i list mo sys --node leaf101.example.com
```

`list class` reads the bundled dictionary, so it needs neither a login nor a
daemon. `list mo` asks the APIC for one level of children, so it needs a session;
without a DN it starts at `uni`, which is the APIC's root -- a switch reached
with `--node` has its own, `sys`.

Both print one name per line and nothing else, so the output of one is the
argument of the next:

```sh
$ a4i list mo uni
uni/infra
uni/tn-common
uni/tn-infra
$ a4i get mo "$(a4i list mo uni | head -1)"
```

Neither is a search: `list class` matches a prefix (case-insensitively) and
`list mo` lists one level, so walking down a tree is one call per level. Nothing
is cached, so what comes back is what the fabric has now.

### Seeing what a post would change

`post --dry-run` sends nothing. It fetches the subtree the body targets and
prints the difference between it and the body:

```sh
a4i post mo uni/tn-demo --dry-run '{"fvTenant":{"attributes":{"descr":"prod"},
  "children":[{"fvBD":{"attributes":{"name":"bd2"}}}]}}'
```

```
~ fvTenant uni/tn-demo
  ~ descr: "" -> "prod"

+ fvBD uni/tn-demo/BD-bd2
  + name: "bd2"

1 created, 1 modified, 0 deleted
```

`+` is a new MO, `~` an MO whose attributes change, `-` an MO the body deletes
with `status="deleted"` (the count of MOs going with its subtree is shown), and
`!` a warning -- a `status="created"` on an MO that already exists, or a body
that leaves out a property its RN is built from, both of which the APIC will
reject. Only what actually changes is printed: a POST leaves every
attribute the body does not mention alone, so an attribute already holding the
value the body sets is not a change.

The exit code says whether the body would do anything:

| Code | Meaning |
| --- | --- |
| `0` | posting this body would change nothing |
| `2` | it would change something, or fail on a warning |
| `1` | the dry run itself failed (not logged in, bad JSON, unknown DN) |

The MO to compare against is the body's `dn` attribute if it has one, otherwise
the target -- so both `a4i post mo uni/tn-demo --dry-run` and `a4i post mo uni
--dry-run` with a `dn` in the body work. A `post class` target with no `dn` in
the body names no MO, and is an error rather than a guess.

Three limits are worth knowing:

- ACI bodies name children by a naming property (`name`, `ip`, ...), not by DN.
  The RN format that turns one into the other is bundled, one per configurable
  class, so a new MO is reported under the DN the APIC would give it. A class
  the bundled dictionary does not know shows under a stand-in RN instead --
  `uni/tn-demo/fooBar[name=b1]` -- and is still correctly reported as new.
- A new MO lists only the attributes the body sets. The APIC's own defaults for
  the rest are not known here.
- Nothing is validated against the APIC's rules. A dry run that reports changes
  can still fail on a value the APIC rejects.

### Detecting configuration differences

`diff` compares an intended configuration against everything the fabric has
under `uni`, and reports both directions: what the configuration asks for and
the fabric lacks, and what the fabric carries and the configuration never
mentions -- the BD someone added by hand.

```sh
a4i diff ./configs/               # every *.json under the directory
a4i diff base.json override.json  # merged in the order given
cat fabric.json | a4i diff -
a4i diff ./configs/ --exclude uni/tn-common --exclude uni/infra
```

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
attributes differ. Attribute lines read the same way: `+` wanted but unset, `-`
set on the fabric and unaccounted for, `~` set to something else. A wholly
missing or wholly extra subtree is reported as its top MO with the MOs below it
counted; `--expand` lists every one of them instead.

The exit code says whether the fabric matches:

| Code | Meaning |
| --- | --- |
| `0` | the fabric matches the configuration |
| `2` | it differs from the configuration |
| `1` | the comparison itself failed (not logged in, bad JSON, an unreadable subtree) |

Inputs are merged in the order they are read -- arguments left to right, and
`*.json` within a directory argument in path order, recursively -- with a later
value winning attribute by attribute. So a tenant can be split across files, and
a file named last on the command line overrides what the directory before it
set. A path named outright is read whatever it is called; only the walk of a
directory looks at the extension.

#### Leaving a subtree out

`--exclude DN` takes that MO and everything under it out of the comparison. It
is repeatable, and holds no separator of its own: an ACI naming value can carry
a comma, so one option is one DN.

```sh
a4i diff ./configs/ --exclude uni/tn-common --exclude uni/tn-mgmt
```

- **Both sides go.** An excluded MO is neither `missing` nor `extra` nor
  `modified`, whichever side happens to carry it: excluding something is saying
  nothing about it, and reporting it missing because the fabric was never
  consulted would be the comparison talking about what it did not look at. The
  count of MOs going with a summarised subtree drops to match.
- **The boundary is an RN, not the text of the DN.** `uni/tn-common` does not
  take `uni/tn-common2` with it, and a DN cut short inside a naming value --
  `uni/tn-a/BD-b/subnet-[10.0.0.1` -- is the ancestor of nothing.
- **A DN that matches nothing is accepted**, so one command line serves a fabric
  that carries the tenant and one that does not. It hides no difference: the
  worst it does is leave the noise it was meant to remove.
- The fabric is still fetched in full. This narrows what is compared, not what
  is read.
- Nothing special is made of `uni`: it is the ancestor of every MO, so
  `--exclude uni` leaves nothing to compare and the diff is always clean.

Six things are worth knowing before pointing this at a fabric:

- **The configuration is taken to describe the whole of `uni`.** Everything it
  leaves out is `extra` -- including `tn-common`, `tn-infra`, `tn-mgmt` and the
  policies under `uni/infra` that the APIC creates for itself. A configuration
  covering one tenant reports the rest of the fabric as extra, by design, and
  `--exclude` above is how the rest is quietened.
- **Attributes are compared both ways as well.** The APIC returns every settable
  property of an MO, unset ones as `""`, so an MO the configuration names
  without spelling out in full is reported property by property.
- `diff` issues GETs and nothing else. Fixing what it finds is `post`, and
  which findings are worth fixing is a judgement it does not make.
- The fabric is fetched one top-level subtree at a time rather than as a single
  `rsp-subtree=full` over `uni`, which a large fabric times out on. If one of
  those fetches fails the whole comparison fails, naming the subtree: a
  difference missed inside a subtree that could not be read would otherwise be
  indistinguishable from a fabric that matches.
- **An MO the input does not name stops the comparison.** ACI bodies name their
  children by a naming property (`name`, `ip`, ...) rather than by DN, and the RN
  format that turns one into the other is bundled: `fvBD` is `BD-{name}`. A body
  that leaves out what its RN is built from names no one MO -- it could be any
  `fvBD` under that tenant -- and `diff` refuses to compare rather than guessing:

  ```
  error: cannot tell which MO the input means by fvBD under uni/tn-demo (its RN
  is "BD-{name}"): the properties an RN is built from are missing. Give it those,
  a "dn" or an "rn".
  ```

  Reporting such an MO missing while reporting the real one extra would be worse
  than saying what the input has to spell out. One under an excluded DN is
  passed over rather than refused: which MO it meant is a question about a
  subtree nothing will be reported about.
- **A class the bundled dictionary does not know is reported under a stand-in
  RN**, naming it after what the input gives -- `+ fooBar uni/tn-demo/fooBar[name=b1]`
  -- rather than stopping the comparison, which a dictionary a release behind the
  fabric would otherwise do. `name` alone stands in where there is one, so an MO
  split across a base and an override still merges; where there is none every
  attribute goes in, and two inputs merge only when they say the very same thing.

A `status` in an input is ignored -- it directs a POST, while a `diff` input
states what the fabric should look like -- so the same JSON serves both
commands. An MO with no `dn` hangs under `uni`, and a body wrapped in `polUni`
is read through to its children whether or not the wrapper carries a `dn`.

### Query options

Every `get` option is named after the ACI query parameter it sets, so a parameter
read in the APIC REST API documentation can be typed as-is:

| Option | Value |
| --- | --- |
| `--query-target` | `self` (the APIC default, sent only when given), `children`, `subtree` |
| `--target-subtree-class` | class names, comma-separated |
| `--query-target-filter` | filter expression, e.g. `eq(fvTenant.name,"common")` |
| `--rsp-subtree` | `no`, `children`, `full` |
| `--rsp-subtree-class` | class names, comma-separated |
| `--rsp-subtree-filter` | filter expression |
| `--rsp-subtree-include` | `faults`, `health`, `stats`, `audit-logs`, `event-logs`, `fault-records`, `health-records`, `relations`, `tasks`, and the modifiers `count`, `no-scoped`, `required`; comma-separated |
| `--rsp-prop-include` | `all`, `naming-only`, `config-only` |
| `--order-by` | `CLASS.PROPERTY[\|asc\|desc]`, e.g. `eventRecord.created\|desc` |
| `--page` / `--page-size` | page number counting from 0, and objects per page |

```sh
a4i get class fvTenant --query-target subtree --target-subtree-class fvAEPg,fvBD
a4i get class fvTenant --rsp-subtree full --rsp-subtree-include faults,no-scoped
a4i get class eventRecord --order-by 'eventRecord.created|desc' --page 0 --page-size 10
```

`--page` and `--page-size` must be given together, since ACI paginates on the
pair. Class names are never validated against the bundled dictionary, so a class
from an APIC newer than the dictionary is still passed through; `a4i list class`
is there when you want to check a spelling.

### Querying a fabric switch directly

Fabric switches accept the token the APIC issued, so `get --node HOST` sends the
query straight to a switch's local MIT without a second login. `HOST` is an IP or
hostname the switch answers on (its OOB or in-band management address); node IDs
are not resolved. The TLS setting from `login` applies to the switch connection
too, and the session still lives on the APIC -- expiry and refresh are unchanged.

`--node` is deliberately available on the commands that only read -- `get` and
`list` -- and not on `post`. A switch's MIT is a projection of the policy the
APIC resolved onto it, so configuration written there is not reflected in the
APIC and is overwritten on the next policy resolution. Configure through the
APIC.

`a4i list mo --node HOST DN` walks the same MIT, so a switch's tree can be
explored the way the APIC's can. Its root is `sys`, not `uni`.

## MCP server

`a4i mcp` speaks the Model Context Protocol on stdin and stdout, so an LLM client
can read and write the fabric through the session you already logged in. Register
it with the client:

```json
{"mcpServers": {"a4i": {"command": "a4i", "args": ["mcp"]}}}
```

Then log in from a terminal as usual. There is no login tool: the password is
typed by a person, and this server never handles one.

No port is opened. `a4i mcp` is a short-lived process that forwards to the same
Unix domain socket a command uses, so the token stays where it was -- in one
process's memory, behind a socket in a directory only its owner can use. Putting
an HTTP listener in the daemon would have been the other way to do this, and it
would have handed the APIC session to anything that could reach the port.

The server starts whether or not anyone is logged in, since a client launches it
when the editor starts. Until a session exists, every tool that needs the fabric
says which command to run.

### Tools

| Tool | What it does |
| --- | --- |
| `search` | find a class by what it is called -- "bridge domain" finds `fvBD` |
| `describe` | one class from the bundled model: settable properties, permitted values, defaults, how its RN is built, what may hang under it |
| `list` | class names by prefix, or the DNs one level under a DN |
| `get` | the `get` above, with every query option under its own name |
| `dry_run` | what a POST would change, sending nothing |
| `post` | the `post` above |
| `diff` | the `diff` above, over inline bodies or paths |

The arguments are the CLI's options with underscores, which are the ACI query
parameter names themselves, so a parameter read in the APIC documentation can be
written as-is. `dry_run` is a tool of its own rather than a flag on `post`,
because a flag is something a model forgets to set.

A `get` whose response would exceed 64 KB is refused, with the total count and
the ways to narrow it. Nothing is truncated and nothing is silently paged: a
result that comes back is the whole of what was asked for. `A4I_MCP_MAX_BYTES`
raises or lowers the limit -- from the environment, deliberately not as a tool
argument, since a limit the caller can raise is one that gets raised the moment
it binds.

On a `--read-only` session, `post` is not offered at all. The daemon would refuse
it anyway; leaving it out of the list says so before a call is spent finding out.
The tool list is built from the daemon's state each time it is asked for, and
when a login changes that state the client is told to ask again.

### What the model is told

Four documents are offered as MCP resources, and the two an LLM cannot work
without are repeated in the server's own instructions -- a resource is offered to
the client rather than read by the model, and a client that never fetches one
would otherwise leave the model to guess:

| Resource | Contents |
| --- | --- |
| `a4i://guide/post-body` | how an ACI body nests, how a child MO gets its DN from a naming property, what `status` does |
| `a4i://guide/query` | class queries against MO queries, the two independent subtree controls, how to keep a response small |
| `a4i://guide/workflow` | the order: search or list, describe, get, dry run, post |
| `a4i://guide/limits` | where the bundled model, the dry run and the diff each stop short |

`describe` is what covers the rest. The bundled model carries every class the MIM
Reference documents: what it is for, its properties with their types, permitted
values, defaults and validation, which classes contain it, and -- for a class
that can be configured -- how its RN is built and which classes may hang under
it. That is the difference between a model that can write a valid body and one
that has to guess property names.

## Python library

The same `get` and `post` are importable. A `Client` holds its own session, so
no daemon is involved: it logs in itself and keeps the token in memory for as
long as it lives.

```python
import a4i

with a4i.Client("apic1.example.com", verify=False) as client:
    client.login("admin", password)

    data = client.get("fvTenant", kind="class", query_target="subtree", rsp_subtree="full")
    print(data["totalCount"])
    for mo in data["imdata"]:
        ...

    data = client.get("l1PhysIf", kind="class", node="leaf101.example.com")

    client.post("uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo")
```

`kind` is the subcommand the CLI takes, and it is required: `"class"` for a class
name, `"mo"` for a DN. Every other `get` keyword argument is the CLI option of the
same name with underscores, so the [table above](#query-options) reads as the
argument list. Class lists take a string or a sequence, `page` and `page_size`
take integers, and `params=` passes a parameter this dictionary does not know
straight through.

```python
client.get("fvTenant", kind="class", target_subtree_class=["fvAEPg", "fvBD"])
client.get("eventRecord", kind="class", order_by="eventRecord.created|desc", page=0, page_size=10)
```

`get` returns the APIC's response exactly as the CLI prints it, `totalCount`
included. `post` takes JSON text -- sent byte for byte -- or an object to
serialize. `node` is available on `get` only, for the reason given above.

`client.dry_run()` is `post --dry-run`: it sends nothing and returns the list of
[`Change`](#seeing-what-a-post-would-change) the POST would cause, empty when it
would change nothing. `client.post(..., dry_run=True)` is the same call.

```python
changes = client.dry_run("uni/tn-demo", {"fvTenant": {"attributes": {"descr": "prod"}}}, kind="mo")
for change in changes:
    print(change.kind, change.class_name, change.dn, change.attributes)
```

`client.diff()` is the `diff` command: it merges the configurations given to
it, in the order given, fetches everything under `uni`, and returns the list of
[`Change`](#detecting-configuration-differences) that says how the two differ --
empty when the fabric matches. Each argument is one ACI body, an MO or a list of
them, so the CLI's directory of files is a list of parsed objects here.

```python
changes = client.diff(base, override, expand=True)
for change in changes:
    print(change.kind, change.dn)  # missing | modified | extra
```

`exclude` is the `--exclude` option: a DN, or a sequence of them, left out of
the comparison along with everything under each. A single string is one DN and
is never split.

```python
changes = client.diff(base, exclude="uni/tn-common")
changes = client.diff(base, exclude=["uni/tn-common", "uni/tn-mgmt"])
```

A value ACI does not define raises `ValueError` before anything is sent. A
failed request raises `a4i.ApicError`, `a4i.NotLoggedInError` or
`a4i.SessionExpiredError`, all of them `a4i.A4iError`. The token refreshes
itself once half its lifetime has elapsed, so a long-running script needs
nothing of its own.

### Async

`AsyncClient` is `Client` awaited: the same arguments, the same return values
and the same exceptions, sending the same requests in the same order. Only the
sending is awaited, so a caller keeps its event loop while the APIC thinks.

```python
import a4i

async with a4i.AsyncClient("apic1.example.com", verify=False) as client:
    await client.login("admin", password)

    data = await client.get("fvTenant", kind="class", query_target="subtree")
    await client.post("uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo")

    changes = await client.diff(base, override)
```

Every method is awaited under the same name, `close()` included; `logged_in`
stays a plain property. `diff` still fetches one subtree per request rather
than all of them at once, so the fabric sees the load the CLI puts on it and a
comparison reports what the CLI's would.

The daemon carries a token across the short-lived processes a CLI run is made
of, which is not a problem an `async with` block has, so there is no awaited
daemon transport: an `AsyncClient` holds a session of its own, as a `Client`
built from a host does.

### The daemon socket

The socket lives in a private directory named after your uid, under
`$XDG_RUNTIME_DIR` when that is set and short enough for an `AF_UNIX` path, and
under `/tmp` otherwise (the usual case on macOS):

```
/tmp/a4i-501/daemon.sock
```

The password for `login` travels over that socket, so both the CLI and the
daemon refuse to use the directory unless it is a directory you own with mode
`0700` -- otherwise, on a shared machine, another user could create it first and
listen there. If a command reports `must be mode 0700` or `is owned by uid ...`,
inspect the directory: either something loosened its permissions, or someone
else got to the name first.

`a4i daemon stop` leaves the empty directory behind on purpose, so the name
stays claimed for the next daemon.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ty check
```
