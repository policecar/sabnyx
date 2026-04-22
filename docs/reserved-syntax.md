# Reserved syntax — ZK provenance (stage 3)

Sabnyx uses HTML-comment round-trip for external IDs and proofs. At
stage 0 this syntax is reserved; at stage 3 it is populated by the
Lemma integration.

Comments live in the body, adjacent to the content they attest:

    <!-- lemma-proof:0xabc... -->
    <!-- lemma-merkle-root:2026-04-22:0xdef... -->
    <!-- source-id:arxiv:2403.12345 -->
    <!-- x402-tx:0x012... -->

The `proof:` field in atlas frontmatter holds the canonical proof
string for the whole file. Body-level comments are for sub-claims.

Round-trip invariant: tools MUST preserve unknown HTML comments
exactly (byte-for-byte) when editing files. The obs-dailynotes
marker-block pattern is the reference implementation.

Pattern taken from `tools/updateRemindersCache.js` in Joi's
obs-dailynotes repo (see docs/recon/obs-dailynotes-reuse.md).
