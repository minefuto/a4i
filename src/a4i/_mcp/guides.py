# What an LLM has to know about ACI before a get or a post of its own is any good: true
# of every class and stated by no per-class record. INSTRUCTIONS repeats the essentials
# of the guides, a resource being offered to the client rather than read by the model.

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

The target is the DN of the body's top MO: an `fvBD` named `bd1` is posted at
`uni/tn-demo/BD-bd1`, and a `polUni` at `uni`. A `dn` in the top MO's
`attributes` wins over the target.

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

## Several bodies, one body

`post` and `diff` each take exactly one body. A configuration written across
several files, or a body of your own laid over an existing configuration, is
folded into that one body by `merge` first. Three rules govern it:

- **Two bodies mean the same MO when they resolve to the same DN.** How each one
  said so does not matter: a `dn`, an `rn`, or a naming property under the same
  parent all arrive at the same key. A body that names no single MO is refused
  here, for the same reason it is refused by `dry_run`.
- **Later wins, attribute by attribute.** An MO given twice is not replaced by
  the later one -- the attributes are laid over each other. So an override file
  need only carry the attributes it changes.
- **`status` merges like any other attribute.** It is kept, so a merged body
  still deletes what the pieces asked to delete. But it is also inherited: if a
  base sets `status: "deleted"` on an MO and a later body sets that MO's `mtu`
  without mentioning `status`, the MO is still deleted. Set `status: ""` -- or
  `"created,modified"` -- to take it back.

The result is a `polUni` holding every merged MO, each nested under the MO it
hangs off and named by its `rn`, siblings in RN order. Post it at `uni`, or give
it to `diff`. Every DN on the way down from `uni` has to be described by
something: a BD written without its tenant is refused rather than nested under a
tenant `merge` made up, and an MO whose DN does not sit under `uni` is refused
too -- post that one on its own. Pass `loose: true` to have the missing ancestor
filled in instead, where the bundled dictionary settles what class sits there;
what it fills in is an MO the post may create that no input asked for, which is
why it is off by default.

## Before you post

Call `dry_run` with the same arguments first. It sends nothing: it fetches what
is there and reports what the POST would change. An empty result means the body
would do nothing at all, which usually means it does not say what you meant.

For a merged configuration, follow that dry run with `plan`. It compares the
same body against the same fetched fabric and hands back a body holding only the
MOs that change, so posting it leaves every MO the fabric already agrees with
untouched instead of handing all of them back to the APIC to be written again.
It returns no report of its own: the dry run you just read is the report.
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

There are two ways through these tools, and the first thing to settle is which
one you are on. Changing one MO, or a handful you can name, is the first.
Bringing the fabric in line with a configuration written down somewhere is the
second. They differ in what they read: the first reads only what your body
names, the second reads the whole of `uni` once and works from that.

## Changing one MO

1. `search` with a plain-language term ("bridge domain", "contract") when you do
   not know the class name, or `list` with `kind: "class"` and a prefix when you
   know how the name begins.
2. `describe` with the class name, always, before writing a body for it. It
   gives you the properties you may set, their permitted values, their defaults,
   what the RN is built from, and which classes may hang under it.
3. `get` with `kind: "mo"` on a DN you know, or `kind: "class"` when you are
   looking for all of something. Reading one real MO of the class you are about
   to write is the surest way to see the shape the fabric actually has -- more
   reliable than any bundled dictionary, because it is this fabric and this
   release.
4. `dry_run` with exactly the arguments you would give `post`. It sends no POST
   and reports what would change. Read it before going on: an empty result means
   your body would change nothing.
5. `post` once the dry run says what you expect.

No `fetch` on this path. `dry_run` uses one if the session happens to hold it,
but reads the few subtrees your body names when it does not, which is far less
than reading the whole of `uni` to check one tenant. Its report says which of
the two it did.

## Applying a whole configuration

1. `merge`, `diff` and `plan` each take the configuration as `paths` (files or
   glob patterns, folded in order, later values winning attribute by attribute)
   or as one inline `body`. Give them `paths`, so a whole fabric's
   configuration never travels through this conversation; `merge` with an
   `output` path writes it out as the one body `dry_run` and `post` take.
2. `fetch` reads the whole of `uni` into the session and returns only how much
   that was. `diff` and `plan` compare against what it read and send nothing to
   the APIC themselves, so one `fetch` serves any number of them.
3. `dry_run` the body at `uni` to read what would change, `diff` to see how the
   fabric and the configuration disagree either way round.
4. `plan` turns the same comparison into a body holding only what changes. It
   prints no report -- the dry run in step 3 is it.
5. `post` the body `plan` returned. It carries the changes that dry run named
   and nothing else, so nothing unread is written.

If `post` is missing from the tool list, this session was started read-only and
nothing can be written through it.

## What the fetched fabric is worth

`post` drops what was read -- it is no longer what the fabric holds -- and so do
a login, a logout and a session expiry. After any of those, `fetch` again. A
`diff` or `plan` with nothing to compare against says so and stops rather than
reading a fabric nobody asked for.

`diff` takes one intended configuration -- a `body` inline, or `paths` to the
files holding it -- and compares it against everything `fetch` read, both ways: what
the configuration asks for and the fabric lacks, and what the fabric carries and
the configuration never mentions.

Note that it treats the configuration as describing the *whole* of `uni`, so a
configuration covering one tenant reports the rest of the fabric as extra. Use
`exclude` to leave subtrees out -- `uni/tn-common`, `uni/tn-infra`,
`uni/tn-mgmt` and `uni/infra` are the usual ones. A `*` in one matches within a
single RN, so `uni/tn-test*` leaves out every tenant named `test...`. A leading
`!` makes one an exception to the others: `["uni/tn-*", "!uni/tn-mgmt"]` compares
that tenant and no other. A trailing `[key=value]` narrows one to the MOs whose
attribute matches, for what a DN cannot tell apart:
`uni/infra/accportprof-*/hports-*[descr=auto-*]`.
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

- **`diff` and `plan` compare against what `fetch` read, not the fabric.** They
  send nothing to the APIC, so a change made between the `fetch` and them is not
  in the comparison. A `post` drops what was read for that reason; a change
  somebody else makes cannot be noticed, so `fetch` again when the answer has to
  be current.

- **`dry_run` compares against the same fetched fabric, when there is one.** It
  falls back to reading the subtrees your body names, which is always current,
  and its report says which of the two it did. So the staleness above is the
  dry run's too whenever it says it used the fetched fabric.

- **`diff` compares the whole of `uni`.** Everything the configuration leaves
  out is reported as extra, including the tenants and infrastructure policies
  the APIC creates for itself. That is by design; `exclude` is how you quieten
  it.

- **An MO that names no single object stops a comparison.** ACI bodies name
  children by a naming property, so a body that omits what the RN is built from
  could mean any sibling. `dry_run` and `diff` refuse rather than guess. Give
  the naming property, a `dn`, or an `rn`.

- **`merge` checks the shape, not ACI.** It reads bodies, resolves each MO's DN
  and lays the attributes over each other. Whether the result is a configuration
  the APIC would accept is not known until `dry_run` or `post`.

- **A malformed element is refused, never skipped.** Every path that reads a
  configuration -- `merge`, `diff`, `dry_run` -- checks the shape first: one
  class name per element mapped to a body of `attributes` and `children`, and
  every attribute value a string (a number is taken as one). `null`, `true`, an
  object or an array as a value is refused, and so is a `get` response handed
  over whole -- pass what is inside its `imdata`. What is reported is the file,
  the position and the parent DN of each element to fix.

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
        "The two paths through these tools: changing one MO, and applying a whole configuration.",
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
4. `dry_run` before every `post`, with the same arguments. It sends no POST and
   reports what would change.
5. `post` only once the dry run says what you expect.

That is the path for changing one MO, and it needs no `fetch`. A whole
configuration goes the other way: `merge` it into one body, `fetch` the fabric
once, `dry_run` it at `uni` to read what changes, then `plan` -- the same
comparison as a body, with no report of its own -- and `post` that body.
`diff` answers how the two disagree without
writing anything. `fetch`, `diff` and `plan` are for that path only; `dry_run`
uses a fetched fabric when there is one and reads what your body names when
there is not, and says which it did.

`post` takes one body. Give `diff` and `plan` the configuration's files as
`paths`, so a whole fabric's configuration never travels through this
conversation.

Writing a body: an MO is `{"className": {"attributes": {...}, "children": [...]}}`.
Children are named by a naming property (`name`, `ip`, ...), not by DN -- `fvBD`
with `{"name": "bd1"}` under `uni/tn-demo` means `uni/tn-demo/BD-bd1`. A body
that omits what the RN is built from names no single MO and will be refused. A
POST leaves unmentioned attributes alone; `status: "deleted"` removes an MO and
its subtree.

If `post` is absent from the tool list, this session was logged in read-only and
nothing can be written through it. `dry_run` and `plan` still work, since they
only read.

The `a4i://guide/...` resources spell all of this out at length.
"""
