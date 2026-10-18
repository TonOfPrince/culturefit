# CultureFit

CultureFit turns a public cultural reference into three directions for a cultural
activation: a place, a brand partner and an artist. The current guided example is
Taylor Swift (artist) with a Chicago context. Only the place lane is geographically
filtered. Brands and artists use aggregate affinity signals.

## Run the complete distribution

Run a complete, key-free fictional example:

~~~sh
python3 -B -S run_culturefit.py --offline
~~~

To connect an already authorized trusted local provider gateway instead:

~~~sh
python3 -B -S run_culturefit.py --gateway-port 8766
~~~

Offline mode creates fresh fictional state each process launch; it makes no external request. Source code is MIT licensed; provider data and access are not granted by that license.

## Run the browser application

Python 3.11+; the application uses only the standard library. Start the local
server with an operator-owned catalogue destination in an existing directory:

~~~sh
python3 web_app.py --catalog /private/path/sources.csv --provider-port 8766
~~~

Open the printed loopback URL. The browser checks the separately configured
gateway without consuming a provider attempt. Use **Build this brief** once to
run the guided example. Three complete typed lanes are required; ambiguous,
missing or mismatched results are not shown. After any planning attempt, there
is no automatic retry. Reloading the page clears displayed results but does not
reset the gateway session.

The gateway owns its provider credential, allowed HTTPS origin and one-shot
state. Without an explicit trusted local gateway port, the planning route stays
unavailable. The standard gateway starts locked; a port or a status response is
not execution authority. The complete distribution includes its required
provider modules; starting this application directory alone does not configure a
provider.

The UI labels fictional offline responses and actual provider HTTP separately.
No API key is accepted by the browser or returned to it. Live mode currently
permits only the separately authorized frozen example; arbitrary live anchor/city
inputs are not enabled.

## Optional local source catalogue

The collapsible source tools import and search your own CSV without provider
requests or visiting source URLs. Use the UTF-8 header:

~~~csv
lane,name,city,tags,source_url,source_label
~~~

Lanes are place, brand and artist. Places require a city. Every record requires
a name, semicolon-separated tags, an HTTPS source URL without credentials and a
source label. Matching uses your exact authored tags; sources remain
user_supplied_unverified. Empty lanes stay empty.

An existing catalogue requires explicit replacement confirmation. Imports are
bounded to 2 MB and use private file permissions with validated atomic replacement.
Stop the server and all other writers before using any separate catalogue editing,
backup or restore CLI. In-process locking is not a cross-process conditional write.

The existing CLI modules also support source coverage, candidate explanations,
three-lane bundles, city comparison, single-record append/correction/removal,
backup/restore, handoff and triage. Optional revision history is excluded; old
revision artifacts are not trusted source-lineage evidence.

## Verification

- The reusable provider composition and standalone HTTP connection have executed
  native integration checks with HTTPS forbidden during offline tests.
- One prospectively authorized frozen live artist/Chicago run returned HTTP200
  for search and three insights requests, one complete card per lane. Repeat409
  caused no extra provider call. Independent inspection checked captured hashes,
  identity/type correspondence, filters and provider-reported Chicago metadata.
- Those observations establish the bounded backend flow. They are not independent
  geographic/quality verification or permission for arbitrary live inputs.
- Browser and source-export acceptance apply to their exact final artifacts and
  are recorded separately. A committed test file is not an execution receipt.

Useful local checks:

~~~sh
node --test tests/test_browser.cjs
python3 -S -m unittest discover -s tests -p 'test_*.py'
~~~

The provider-status test additionally requires the complete distribution's
provider modules and fixture helper. Public distributions must include all
required runtime source and an explicit source license, never a credential,
operator catalogue, private provider response or internal operational record.

## Interpretation

Qloo results describe aggregate cultural affinities. They are starting points,
not endorsements, identity/sensitive-trait inferences, predictions about an
individual, or causal conclusions. Verify local availability and suitability
before planning an activation.

The Python application has no third-party runtime dependencies. Run the optional JavaScript regression tests with Node.js 18 or later; Node.js is not needed to start the Python application.

## Hosted offline demo

The hosted entry point uses `hosted/index.html`, `ui/hosted-demo.js` and the standard-library Python function `api/demo.py`. Its banner and cards explicitly identify fictional responses; it never contacts Qloo and has no provider credential. Each calculation creates a temporary copy of the current catalogue and discards it afterwards. Catalogue CSV and the single-attempt flag stay in this browser tab's session storage. Use public or fictional records only. Closing the tab starts a fresh demonstration session.

The hosted adapter invokes the same local HTTP application, provider gateway, strict agent and four-response offline transport as the complete distribution. This hosted demonstration does not establish a live hosted Qloo integration. The separately verified frozen live example remains an operator-authorized local gateway path.

Deploy this complete directory as a Vercel Python project with the included `vercel.json` and Python 3.14 requirement. No external packages, environment secrets or paid integrations are required. Additional checks:

~~~sh
node --test tests/test_hosted_demo.cjs
python3 -B -S -m unittest discover -s tests -p 'test_hosted_demo.py'
~~~
