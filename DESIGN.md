# Bolo dashboard design
Bolo uses a compact native macOS dashboard for recovering words and adjusting dictation.
The existing Bolo mark and wordmark retain the warm paper, ink and clay identity.
System sans text and SF Symbols make controls familiar and keep long transcripts readable.
Home shows actual cumulative counts with their scope, five recent dictations and learned words.
Dictations uses a scrolling history list beside a selectable transcript, Copy and raw/clean controls.
Settings groups native selectors and explains which saved changes require a restart.
Light and dark previews use synthetic data; live clicks and dictation speed are separate checks.

The dashboard refines an existing visual identity in Operate mode. Its purpose is to recover saved words, copy them, and adjust dictation. It is a native AppKit surface, so HTML and CSS design detectors do not apply.

Typography: 22pt system sans headings, 14/15pt section headings, 13pt body and controls, 11/12pt metadata. The serif wordmark is the only serif treatment. Canvas and sidebar use neutral ivory or charcoal; selected navigation and history rows use a clay tint. Primary Save uses the brand clay fill. Secondary actions use quiet neutral fills.

At the 940 by 650pt minimum content size, the sidebar is 184pt wide and the main surface has 28pt side margins. The history list is 260pt wide; the detail panel takes the remaining readable width. Full history and long transcripts scroll independently. Larger windows preserve a readable content width and provide extra vertical room.

A fresh GLM-5.3-Flash review examined the actual native screenshots. Follow-up changes tightened settings spacing, removed detached history dividers, reduced the empty-state surface, grouped usage counts more closely and moved transcript actions above the text. A bottom-scrolled preview and a native glyph-visibility check cover long transcript reachability. Both count baselines, the shortcut hint and learned-word labels were already aligned in the native view frames; these did not need invented companion claims or baseline offsets.

Preserve the current event protocol and settings drafts across navigation. Preview capture must not activate the app, write setup markers or read the user's saved transcripts. Product context is in PRODUCT.md; real measurements are required before adding latency or time-saved claims.
