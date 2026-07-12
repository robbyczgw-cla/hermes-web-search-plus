// Amendment 002 JSON-schema boundary check (Ajv).
// Exit 0 only when the generated response schema enforces the amendment shape
// and all six golden fixtures validate. Expected RED until the schema
// generator implements Amendment 002 (generator itself is out of scope here).
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(
  "/root/work/web-search-plus-plugin/node_modules/",
);
const Ajv = require("ajv/dist/2020");

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const schema = JSON.parse(
  readFileSync(join(root, "schemas/v3/response.schema.json"), "utf-8"),
);
const ajv = new Ajv({ strict: false, allErrors: true });
ajv.addFormat("uri", {
  type: "string",
  validate(value) {
    try {
      const parsed = new URL(value);
      return Boolean(parsed.protocol && parsed.hostname);
    } catch {
      return false;
    }
  },
});
ajv.addFormat("date-time", {
  type: "string",
  validate(value) {
    return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
      && !Number.isNaN(Date.parse(value));
  },
});
const validate = ajv.compile(schema);

const failures = [];

const FIXTURES = [
  "01_search_success", "02_extract_success", "03_cache_hit",
  "04_fallback", "05_degraded", "06_total_failure",
];

// 1. All six golden fixtures must validate.
for (const name of FIXTURES) {
  const doc = JSON.parse(
    readFileSync(join(root, `tests/fixtures/v3/${name}.json`), "utf-8"),
  );
  if (!validate(doc)) {
    failures.push(`${name}: ${ajv.errorsText(validate.errors)}`);
  }
}

// 2. Schema must REQUIRE the amendment fields: stripping them must invalidate.
const base = JSON.parse(
  readFileSync(join(root, "tests/fixtures/v3/01_search_success.json"), "utf-8"),
);
for (const required of ["execution_id", "observations", "policy_actions", "source_diversity"]) {
  const mutated = JSON.parse(JSON.stringify(base));
  delete mutated[required];
  if (validate(mutated)) {
    failures.push(`schema does not require ${required}`);
  }
}

// 3. Schema must REJECT the removed legacy field and diversity scalars.
const withLegacy = JSON.parse(JSON.stringify(base));
withLegacy.source_independence_estimate = { score: 0.7 };
if (validate(withLegacy)) {
  failures.push("schema still accepts source_independence_estimate");
}
const withScalar = JSON.parse(JSON.stringify(base));
withScalar.source_diversity.scalar = 0.8;
if (validate(withScalar)) {
  failures.push("schema accepts a source_diversity scalar (additionalProperties)");
}

// 4. Partial engine object must be invalid; complete one valid.
const partialEngine = JSON.parse(JSON.stringify(base));
partialEngine.engine = { name: "wsp", version: "3.0" };
if (validate(partialEngine)) {
  failures.push("schema accepts a partial engine object");
}
const fullEngine = JSON.parse(JSON.stringify(base));
fullEngine.engine = { name: "wsp", version: "3.0", build_commit: "deadbeef" };
if (!validate(fullEngine)) {
  failures.push(`schema rejects a complete engine object: ${ajv.errorsText(validate.errors)}`);
}

// 5. Format constraints are executable, not ignored annotations.
const invalidUri = JSON.parse(JSON.stringify(base));
invalidUri.results[0].url.observed = "not a uri";
if (validate(invalidUri)) {
  failures.push("schema accepts an invalid observed URI");
}
const invalidDateTime = JSON.parse(JSON.stringify(base));
invalidDateTime.started_at = "yesterday-ish";
if (validate(invalidDateTime)) {
  failures.push("schema accepts an invalid date-time");
}

if (failures.length) {
  console.error("SCHEMA BOUNDARY FAILURES:\n" + failures.join("\n"));
  process.exit(1);
}
console.log("schema boundary: all checks passed");
