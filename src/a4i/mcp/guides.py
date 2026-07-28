"""What an LLM has to know about ACI before a get or a post of its own is any good.

The bundled dictionary answers questions about one class. These answer the ones
that come before that: how an ACI body nests, what a DN is made of, which query
to reach for, and what this tool will not tell you. Nothing here is generated --
it is the part of the model that no per-class record states, because it is true
of all of them.

:data:`INSTRUCTIONS` is the summary the server hands over at ``initialize``. It
repeats the two guides an LLM cannot work without, because a resource is offered
to the client rather than read by the model, and a client that never fetches one
would otherwise leave the model to guess.
"""

from __future__ import annotations

POST_BODY = """\
# Writing an ACI POST body

Every managed object (MO) is one JSON key -- its class name -- wrapping
`attributes` and, optionally, `children`:

```json
{"fvBD": {"attributes": {"name": "bd1", "mtu": "9000"},
          "children": [{"fvSubnet": {"attributes": {"ip": "10.0.0.1/24"}}}]}}
```

## Where the body lands

`post` takes a `kind` and a `target`, exactly as `get` does:

- `kind: "mo"`, `target: "uni/tn-demo"` posts the body at that DN.
- `kind: "class"`, `target: "fvTenant"` posts to the class endpoint; the body
  then has to carry a `dn` of its own.

The target is the parent the body hangs under. A body posted at `uni/tn-demo`
whose top MO is `fvBD` creates `uni/tn-demo/BD-bd1`.

## How a child MO gets its DN

This is the part that catches people out: **a body names its children by a
naming property, not by DN.** `fvBD` is named by `name`, and its RN format is
`BD-{name}`, so `{"name": "bd1"}` under `uni/tn-demo` means
`uni/tn-demo/BD-bd1`.

Use `describe` to see a class's `rn` (its RN format) and `naming` (the
properties the RN is built from). A body that leaves out what the RN needs names
no single MO, and `dry_run` and `diff` will say so rather than guess.

You may also give `dn` or `rn` outright in `attributes`, and that wins over the
RN format. Doing so is the way to write several unrelated MOs in one body.

## status

`status` in `attributes` directs what the POST does:

- absent -- create the MO if it is not there, otherwise modify it. This is the
  usual case and what you want most of the time.
- `"created"` -- create, and fail if it already exists.
- `"modified"` -- modify, and fail if it does not exist.
- `"deleted"` -- delete the MO and everything under it.
- `"created,modified"` -- the same as absent, spelled out.

A POST leaves every attribute the body does not mention alone. To clear an
attribute you must set it to `""`, not omit it.

## Before you post

Call `dry_run` with the same arguments first. It sends nothing: it fetches what
is there and reports what the POST would change. An empty result means the body
would do nothing at all, which usually means it does not say what you meant.
"""


QUERY = """\
# Querying ACI

## class or mo

- `kind: "class"` asks for every MO of a class fabric-wide: `fvTenant` returns
  all tenants. Use it when you know what kind of thing you want but not where.
- `kind: "mo"` asks for one MO by its DN: `uni/tn-common`. Use it when you know
  exactly which object you mean.

`list` answers the two questions those raise: `list` with `kind: "class"` and a
prefix says which classes exist, `list` with `kind: "mo"` and a DN says what
hangs under it, one level at a time. `search` finds a class by what it is called
("bridge domain") when you do not know how its name begins.

## The two subtree controls are independent

- `query_target` widens **what is queried**: `self` (default), `children`,
  `subtree`. With `subtree`, the scope becomes the target and everything below
  it, and `target_subtree_class` / `query_target_filter` narrow that scope.
- `rsp_subtree` widens **what comes back for each MO in scope**: `no` (default),
  `children`, `full`. `rsp_subtree_class` / `rsp_subtree_filter` narrow that.

They compose. "Every BD under this tenant, with its subnets" is
`query_target: "subtree"`, `target_subtree_class: "fvBD"`,
`rsp_subtree: "full"`.

## Keeping responses small

Responses from a real fabric are large, and this server refuses one over its
size limit rather than flooding your context. Three things shrink them:

- `rsp_prop_include: "config-only"` drops every read-only property. On a big
  tree this alone is often an order of magnitude, and it is what you want when
  the question is about configuration.
- `rsp_prop_include: "naming-only"` drops everything but the naming properties
  and the DN -- enough to find out what exists.
- `page` and `page_size` must be given together; `page` counts from 0.

Also prefer `target_subtree_class` over fetching a subtree and filtering it
yourself.

## Filters

`query_target_filter` and `rsp_subtree_filter` take ACI filter expressions:
`eq(fvTenant.name,"common")`, `ne(...)`, `lt`, `gt`, `le`, `ge`, `wcard`,
`and(...)`, `or(...)`, `not(...)`.

## Querying a switch

`node` sends the query straight to a fabric switch's local MIT using the same
token, and takes the switch's management IP or hostname -- not a node ID. Its
root is `sys`, not `uni`. It is available on `get` and `list` and deliberately
not on `post`: a switch's MIT is a projection of what the APIC resolved onto it,
so anything written there is overwritten at the next policy resolution.
"""


WORKFLOW = """\
# Working with this fabric

## Finding your way to a class

1. `search` with a plain-language term ("bridge domain", "contract") when you do
   not know the class name.
2. `list` with `kind: "class"` and a prefix when you know how the name begins.
3. `describe` with the class name, always, before writing a body for it. It
   gives you the properties you may set, their permitted values, their defaults,
   what the RN is built from, and which classes may hang under it.

## Reading

4. `get` with `kind: "mo"` on a DN you know, or `kind: "class"` when you are
   looking for all of something. Reading one real MO of the class you are about
   to write is the surest way to see the shape the fabric actually has -- more
   reliable than any bundled dictionary, because it is this fabric and this
   release.

## Writing

5. `dry_run` with exactly the arguments you would give `post`. It sends nothing
   and reports what would change. Read it before going on: an empty result means
   your body would change nothing.
6. `post` once the dry run says what you expect.

If `post` is missing from the tool list, this session was started read-only and
nothing can be written through it.

## Comparing a whole fabric

`diff` takes an intended configuration -- inline objects, or paths to JSON files
and directories -- and compares it against everything under `uni`, both ways: what
the configuration asks for and the fabric lacks, and what the fabric carries and
the configuration never mentions.

Note that it treats the configuration as describing the *whole* of `uni`, so a
configuration covering one tenant reports the rest of the fabric as extra. Use
`exclude` to leave subtrees out -- `uni/tn-common`, `uni/tn-infra`,
`uni/tn-mgmt` and `uni/infra` are the usual ones.
"""


LIMITS = """\
# What this tool does not know

- **The bundled dictionary may be older than the fabric.** Class names are never
  validated against it, so a class it has never heard of still queries fine.
  `describe` returning nothing means the dictionary lacks the class, not that
  the class does not exist.

- **A dry run is not validation.** It compares your body against what is there
  and reports the difference. It does not check values against the APIC's own
  rules, so a dry run that reports changes can still fail on a value the APIC
  rejects.

- **A dry run cannot show the APIC's defaults.** For an MO that does not exist
  yet it lists only the attributes your body sets. Whatever the APIC would fill
  in for the rest is not known here.

- **`diff` compares the whole of `uni`.** Everything the configuration leaves
  out is reported as extra, including the tenants and infrastructure policies
  the APIC creates for itself. That is by design; `exclude` is how you quieten
  it.

- **An MO that names no single object stops a comparison.** ACI bodies name
  children by a naming property, so a body that omits what the RN is built from
  could mean any sibling. `dry_run` and `diff` refuse rather than guess. Give
  the naming property, a `dn`, or an `rn`.

- **Responses are capped.** A `get` whose response exceeds this server's size
  limit is refused, with the total count and the ways to narrow it. Nothing is
  silently truncated and nothing is silently paged: if you got a result, it is
  the whole of what you asked for.

- **`node` reads a switch, and only reads.** Configuration written to a switch's
  MIT is overwritten at the next policy resolution, so `post` has no `node`.
"""


GUIDES: dict[str, tuple[str, str, str]] = {
    # uri suffix -> (title, description, text)
    "post-body": (
        "Writing an ACI POST body",
        "How an ACI body nests, how a child MO gets its DN, and what status does.",
        POST_BODY,
    ),
    "query": (
        "Querying ACI",
        "class vs mo, the two subtree controls, and how to keep a response small.",
        QUERY,
    ),
    "workflow": (
        "Working with this fabric",
        "The order to use these tools in: find, describe, read, dry run, post.",
        WORKFLOW,
    ),
    "limits": (
        "What this tool does not know",
        "Where the bundled model, the dry run and the diff each stop short.",
        LIMITS,
    ),
}


INSTRUCTIONS = """\
This server talks to a Cisco ACI fabric through a running a4i daemon, which
holds the APIC session token. Log in from a terminal with `a4i login` -- there is
no tool for it here.

Work in this order:

1. `search` (plain language) or `list` with `kind: "class"` (name prefix) to find
   a class name.
2. `describe` on that class before writing anything for it: it gives the
   settable properties, their permitted values and defaults, what the RN is
   built from, and what may hang under it.
3. `get` to read. `kind: "class"` for every MO of a class, `kind: "mo"` for one
   DN. Add `rsp_prop_include: "config-only"` when the question is about
   configuration -- responses over the size limit are refused, not truncated.
4. `dry_run` before every `post`, with the same arguments. It sends nothing and
   reports what would change.
5. `post` only once the dry run says what you expect.

Writing a body: an MO is `{"className": {"attributes": {...}, "children": [...]}}`.
Children are named by a naming property (`name`, `ip`, ...), not by DN -- `fvBD`
with `{"name": "bd1"}` under `uni/tn-demo` means `uni/tn-demo/BD-bd1`. A body
that omits what the RN is built from names no single MO and will be refused. A
POST leaves unmentioned attributes alone; `status: "deleted"` removes an MO and
its subtree.

If `post` is absent from the tool list, this session was logged in read-only and
nothing can be written through it. `dry_run` still works, since it only reads.

The `a4i://guide/...` resources spell all of this out at length.
"""
