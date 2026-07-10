# Patent Disclosure Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a fresh, technically accurate Chinese patent disclosure in the specified industrial patent-center template, with editable native Word/WPS formulas and WPS-verified layout.

**Architecture:** Draft the disclosure from verified repository behavior and fresh prior-art research, express the invention as a prediction-evidence and execution-gating feedback loop, then build a template-preserving DOCX. Formula markers are converted into native OMML equation objects through the WPS automation interface, and the completed document is structurally audited and visually reviewed page by page after WPS PDF export.

**Tech Stack:** UTF-8 Markdown, Mermaid, project Windows virtual environment, `python-docx`, OOXML/OMML, WPS `KWPS.Application` COM automation, `pypdfium2`, `pdfplumber`, Google Patents and CNIPA public sources.

## Global Constraints

- Do not reuse the previous disclosure's prose, formulas, examples, protection points, or search conclusions.
- Use only behavior supported by the repository and verified public sources.
- Preserve the supplied template's header, logo, watermark, first-page information block, and six-section organization.
- Leave unknown inventor and applicant fields blank; do not invent identity information.
- Store K8S current specifications and targets at container granularity in `spec.containers`.
- Preserve fractional CPU and memory granularity for K8S targets below `2C/2Gi`.
- Check action, confidence, data quality, cooldown, policy tier, and target readiness before creating an execute task.
- Do not automate OpenStack disk shrink.
- Create editable native OMML formulas; do not use equation screenshots or plain-text substitutes.
- Use WPS, not LibreOffice, for final document rendering and visual review.
- Preserve all earlier disclosure files and write new timestamped artifacts.

---

### Task 1: Establish the Verified Technical Fact Base

**Files:**
- Read: `README.md`
- Read: `docs/architecture.md`
- Read: `docs/configuration.md`
- Read: `resource_predict/pipeline/model_selection.py`
- Read: `resource_predict/pipeline/fit.py`
- Read: `resource_predict/core/decision.py`
- Read: `resource_predict/core/k8s_workload_decision.py`
- Read: `resource_predict/services/urgency.py`
- Read: `resource_predict/pipeline/action_gate_state.py`
- Read: `resource_predict/services/scaling/tasks.py`
- Read: `resource_predict/services/scaling/executor.py`
- Read: `tests/test_action_gate_state.py`
- Read: `tests/test_k8s_workload_decision.py`
- Read: `tests/test_scaling_tasks.py`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/fact_matrix.md`

**Interfaces:**
- Consumes: repository implementation and tests.
- Produces: a fact matrix with columns `technical_feature`, `source_file`, `source_symbol`, `verified_behavior`, and `patent_use`.

- [ ] **Step 1: Extract the concrete prediction, decision, gating, execution, and feedback behavior**

Use `rg` to locate model scoring, anomaly routing, container target construction, action gates, cooldown state, task creation, command construction, and snapshot update symbols. Record exact file and function names in the fact matrix.

- [ ] **Step 2: Separate implemented facts from proposed embodiments**

Mark each row as either `implemented` or `optional embodiment`. Do not state an optional embodiment as current product behavior.

- [ ] **Step 3: Verify project terminology and hard constraints**

Confirm that the fact matrix uses Workload for project concepts, Pod only for Kubernetes labels or cited prior art, and container-level current specifications for multi-container K8S resources.

- [ ] **Step 4: Review the fact matrix for unsupported claims**

Run:

```powershell
rg -n "显著提高|大幅降低|完全避免|保证|最优|生产规模" "$env:TEMP/resource_predict_patent_redesign_20260710101307/fact_matrix.md"
```

Expected: every match is either removed or backed by a source location and a limited technical meaning.

### Task 2: Perform Fresh Prior-Art Research

**Files:**
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/prior_art_notes.md`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/prior_art_matrix.md`

**Interfaces:**
- Consumes: invention concept and fact matrix from Task 1.
- Produces: verified prior-art entries and a closest-prior-art difference matrix used by Sections 2 and 3.

- [ ] **Step 1: Search prediction evidence and multi-model routing**

Search CNIPA and Google Patents for cloud-resource multi-model forecasting, model routing, rolling backtest selection, forecast uncertainty, and traceable prediction evidence.

- [ ] **Step 2: Search execution safety and scaling coordination**

Search for autoscaling confidence, risk priority, execution gates, cooldown control, container vertical and horizontal scaling coordination, and multi-container resource adjustment.

- [ ] **Step 3: Verify every selected entry**

For each entry record publication number, exact title, priority date, publication date, abstract-derived technical means, application scenario, limitation, and a working public URL. Exclude any item whose bibliographic data cannot be verified.

- [ ] **Step 4: Build the inventive-step matrix**

Use columns `closest_reference`, `shared_features`, `distinguishing_features`, `technical_effect_of_difference`, `actual_technical_problem`, and `combination_motivation`. Treat the prediction-evidence, target-generation, confidence/urgency, execution-gate, and feedback relationships as a whole where they jointly produce an effect.

- [ ] **Step 5: Check the search notes for internal process leakage**

Ensure the final disclosure wording does not mention scripts, agents, browser automation, fallback searches, or internal repository names.

### Task 3: Draft the Six-Section Disclosure and Formula System

**Files:**
- Create: `outputs/patent_disclosure/一种基于预测证据链与多重执行门控的异构云资源调配方法及系统_20260710101307.md`

**Interfaces:**
- Consumes: Task 1 fact matrix and Task 2 prior-art matrix.
- Produces: complete UTF-8 Markdown with six template sections, three Mermaid diagrams, formula markers, symbol tables, parameter tables, and two numerical embodiments.

- [ ] **Step 1: Write Sections 1 through 3 as a causal chain**

Section 1 defines technical problems only. Section 2 explains the technical background and verified closest prior art. Section 3 maps each prior-art limitation to the technical cause and corresponding invention objective.

- [ ] **Step 2: Write the system data model and processing flow**

Define the resource—metric—model—window prediction-evidence object, VM and K8S target-specification objects, action-specific urgency objects, execution-gate vector, and feedback state.

- [ ] **Step 3: Define the native formula set**

Create twelve to fifteen numbered formulas covering model score, anomaly/history route, confidence, service-risk urgency, waste urgency, VM target capacity, container request/limit targets, coordination, confidence-urgency routing, six execution gates, and feedback state update. Define every symbol before first use and state dimensional constraints.

- [ ] **Step 4: Write the OpenStack numerical embodiment**

Show inputs, candidate-model errors, selected model, forecast statistics, confidence, service-risk urgency, target flavor, all six gates, execution command, and post-execution state. Prohibit automatic disk shrink.

- [ ] **Step 5: Write the K8S multi-container numerical embodiment**

Show separate current and target request/limit values for at least two containers, fractional target alignment, vertical/horizontal coordination, confidence, action-specific urgency, all six gates, command generation, and feedback.

- [ ] **Step 6: Write protection points and technical advantages**

Section 5 protects the closed-loop relationships and dependent variants. Section 6 states only effects derivable from the disclosed mechanism and numerical examples.

- [ ] **Step 7: Add three fenced Mermaid diagrams**

Add a system architecture diagram, a prediction-evidence and target-generation flow, and a confidence-urgency plus execution-gate flow. Use Chinese labels and monochrome styling.

- [ ] **Step 8: Run textual consistency checks**

Run:

```powershell
rg -n "TBD|TODO|待补|待定|示例文字|Agent|Playwright|cnipa_epub_search|虚构|大幅提高|完全避免" "outputs/patent_disclosure/一种基于预测证据链与多重执行门控的异构云资源调配方法及系统_20260710101307.md"
```

Expected: no unresolved placeholder or internal-process text; any strong-effect wording is rewritten as a bounded technical effect.

### Task 4: Build the Template-Preserving DOCX with Native OMML

**Files:**
- Read: `C:/Users/czh/Desktop/1、工业和信息化部电子专利中心--技术交底书模板.doc`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/template.docx`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/build_disclosure.py`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/diagrams/system_architecture.png`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/diagrams/evidence_target_flow.png`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/diagrams/gating_feedback_flow.png`
- Create: `outputs/patent_disclosure/一种基于预测证据链与多重执行门控的异构云资源调配方法及系统_20260710101307.docx`

**Interfaces:**
- Consumes: Task 3 Markdown, the supplied `.doc` template, and three rendered diagrams.
- Produces: a template-preserving DOCX with real headings, explicit table geometry, native OMML equations, captions, page numbers, and editable body content.

- [ ] **Step 1: Convert a copy of the legacy template through WPS**

Open the supplied `.doc` as read-only using `KWPS.Application`, save a temporary `.docx`, and close WPS. Verify the temporary file contains header relationships and at least one image relationship.

- [ ] **Step 2: Render the three monochrome Mermaid diagrams**

Render at a resolution suitable for A4 portrait placement. Confirm that all labels remain legible when the image width is limited to the document text width.

- [ ] **Step 3: Build the document body from the converted template**

Preserve the template's section, header, logo, watermark, and footer parts. Remove the old body content except `w:sectPr`, then add the new title block, blank identity fields, six sections, tables, diagrams, captions, and formula markers. Apply explicit paragraph spacing, keep-with-next headings, repeat table headers, and non-fixed row heights.

- [ ] **Step 4: Convert every equation marker to native OMML through WPS**

For each marker range, replace the marker with the corresponding linear equation source, add the range to `OMaths`, and call `BuildUp()`. Add a right-aligned equation number in the same paragraph without converting the number into the equation object.

- [ ] **Step 5: Save and reopen the DOCX through WPS**

Reopen the output document read-only and confirm that WPS reports the expected equation count and can access each equation range without error.

### Task 5: Run Structural and Mathematical QA

**Files:**
- Read: `outputs/patent_disclosure/一种基于预测证据链与多重执行门控的异构云资源调配方法及系统_20260710101307.md`
- Read: `outputs/patent_disclosure/一种基于预测证据链与多重执行门控的异构云资源调配方法及系统_20260710101307.docx`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/structural_audit.json`

**Interfaces:**
- Consumes: completed Markdown and DOCX.
- Produces: a passing audit for equations, formulas, figures, tables, links, placeholders, and template preservation.

- [ ] **Step 1: Audit OOXML structure**

Verify `word/document.xml` contains at least twelve `m:oMath` or `m:oMathPara` objects, no equation-marker strings, three figure relationships, header relationships, and no tracked changes or comments.

- [ ] **Step 2: Audit formula consistency**

For each numbered formula, verify all symbols exist in the symbol table, weight constraints are stated, units agree, inequality directions match the prose, and zero denominators or empty sets have explicit handling.

- [ ] **Step 3: Recompute both numerical embodiments independently**

Use the stated inputs and formulas to recompute intermediate values, thresholds, targets, gates, and final paths. Correct the Markdown and DOCX together if any value differs.

- [ ] **Step 4: Audit prior-art citations**

Confirm every patent number, exact title, date, and link in the DOCX matches the verified source notes.

- [ ] **Step 5: Audit document cleanliness**

Confirm there are no template instructions, red drafting notes, repository paths, internal tool names, unsupported effect claims, or invented identity fields.

### Task 6: Perform WPS Visual QA and Finalize the Revision Record

**Files:**
- Read: `outputs/patent_disclosure/一种基于预测证据链与多重执行门控的异构云资源调配方法及系统_20260710101307.docx`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/final.pdf`
- Create: `%TEMP%/resource_predict_patent_redesign_20260710101307/pages/page-*.png`
- Modify: `outputs/patent_disclosure/交底书修订对话记录.md`

**Interfaces:**
- Consumes: structurally passing DOCX.
- Produces: visually verified final DOCX and an appended correction-iteration record.

- [ ] **Step 1: Export the final DOCX to PDF through WPS**

Use `KWPS.Application.ExportAsFixedFormat` and verify the PDF is non-empty.

- [ ] **Step 2: Render every PDF page to PNG**

Use bundled `pypdfium2` at a scale sufficient to inspect formulas, tables, diagrams, headers, footers, and Chinese glyphs.

- [ ] **Step 3: Inspect every page at full resolution**

Check for clipping, overlap, orphan headings, split formula-number pairs, blank pages, unreadable diagram labels, broken table rows, inconsistent fonts, and missing template furniture.

- [ ] **Step 4: Correct and repeat until clean**

For every defect, patch the DOCX source, rebuild native equations if affected, export again through WPS, and reinspect all pages.

- [ ] **Step 5: Append the revision dialog record**

Record local and UTC time, correction-iteration type, the user's request to rebuild from zero with the specified template and native equations, both artifact filenames, and a concise correction summary.

- [ ] **Step 6: Final handoff**

Return links to the final Markdown and DOCX only, state that all formulas are native editable OMML and that all pages passed WPS visual review, and include the required correction-summary and claim-focus guidance.
