# IOSG Relationship Graph

Standalone, API-first introduction-path service extracted from `IOSG-vc/X-deal-sourcing`.
The initial slice preserves the source project's relationship ranking and its privacy-safe
use of Twenty email/calendar metadata, then adds uniform edge-level provenance and support
for company names, domains, project X handles, and founder X handles.

## Architecture

`POST /v1/introduction-paths` calls a small domain service over a `GraphRepository` port.
The checked-in JSON adapter is both an executable demo and the normalized contract that
source sync adapters must produce. Ranking uses confidence-weighted paths with a hop
penalty. Twenty referrals and email/calendar metadata should receive high confidence;
direct CRM relationships medium/high confidence; public social evidence medium confidence;
and an X follow is capped as weak evidence.

The migrated concepts come from:

- `e17cb86` / `27fec05`: Twenty relationship reader, ranked paths, owner resolution, diagnostics
- `2a594d4`: Surf company-person edges with role, source, confidence, and active history
- `c3cfa6c` / `d740d47`: confirmed founder association before using IOSG follow evidence

The next adapters plug into the same repository boundary: Twenty CRM, Neon normalized
tables, Sorsa/TweetScout profile/follow resolution, and Surf company membership. Credentials
remain environment-only. Message bodies, subjects, calendar titles, descriptions, and private
message contents are outside the graph contract.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn relationship_graph.api:app --reload
```

Open `http://localhost:8000` for the web interface. Integration credentials are configured
server-side through environment variables and are never requested by the browser app.

```bash
curl -s http://localhost:8000/v1/introduction-paths \
  -H 'content-type: application/json' \
  -d '{"query":"project.xyz","limit":5}'
```

To query live Twenty data instead of the demo fixture, set:

```dotenv
RELATIONSHIP_GRAPH_BACKEND=twenty
TWENTY_BASE_URL=https://your-twenty-instance.example.com
TWENTY_API_KEY=...
```

Restart Uvicorn after changing `.env`. The Twenty adapter resolves companies by name,
domain, project X handle, or founder X handle. It builds direct introduction edges from
existing introducers and email/calendar participant metadata. Its GraphQL selections
intentionally exclude message bodies, subjects, calendar titles, and descriptions.

When `SURF_API_KEY` is set, project-X searches also add explicit Surf team-role edges.
When `NEON_DATABASE_URL` (or the backward-compatible `DATABASE_URL`) is set, matching founder/team handles are enriched with IOSG follows
from `deals.iosg_x_following`. Those snapshots are expected to be synchronized from Sorsa
out of band; the request path does not download complete following lists. PostgreSQL support
is included in the standard installation; use `pip install -e '.[dev]'` for development tools.

To inspect the raw Surf responses used by the app (`search`, `team`, and `funding`), run:

```bash
python scripts/inspect_surf.py eigenlayer
python scripts/inspect_surf.py eigenlayer --field search
python scripts/inspect_surf.py @eigenlayer --field team
python scripts/inspect_surf.py eigenlayer --field funding
```

The script reads `SURF_API_KEY` from `.env` and never prints the credential.

To inspect a fund's raw Surf identity, team, and portfolio responses, run:

```bash
python scripts/inspect_surf_fund.py "Polychain Capital"
```

If `RELATIONSHIP_API_BASE_URL` and `RELATIONSHIP_API_KEY` are both configured,
person-level relationship ownership from that service takes precedence over inferred
Twenty interaction ownership. A missing record or transient owner-service failure falls
back to Twenty metadata.

## Response semantics

`recommended` is the highest-scoring path, `alternatives` contains the remaining ranked
paths, and each edge includes `relationship`, numeric `confidence`, human-readable
`evidence`, `evidence_source`, and observation time. Diagnostics expose source coverage and
resolution/path counts. A follow path's suggested action explicitly asks the IOSG member to
validate warmth before an introduction is requested.

Operational endpoints:

- `GET /health` checks process health.
- `GET /ready` checks that the selected backend has required local configuration.
- `GET /v1/diagnostics/sources` reports source configuration and Neon/Sorsa freshness.

## Container

```bash
docker build -t iosg-relationship-graph .
docker run --env-file .env -p 8000:8000 iosg-relationship-graph
```

## Verify

```bash
pytest
```
