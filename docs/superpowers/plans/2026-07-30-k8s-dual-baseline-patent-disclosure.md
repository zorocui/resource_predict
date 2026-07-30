# K8S Dual-Baseline Patent Disclosure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a technically complete Chinese patent disclosure DOCX from the retained Ministry template, with verified prior art, editable native Word equations, and six monochrome technical figures.

**Architecture:** Work in a task-local temporary directory, distill the retained DOCX into an `artifact.md` contract, build evidence-backed section content, and generate the final document from a byte-for-byte working copy of the template. Use MathML transformed through Microsoft Office's `MML2OMML.XSL` for native equations, Pillow for high-resolution monochrome figures, structural OOXML validators for deterministic checks, and a Word/PDF render loop for page-by-page visual QA.

**Tech Stack:** Bundled Codex Python 3, `python-docx`, `lxml`, Pillow, Microsoft Office `MML2OMML.XSL`, Microsoft Word 16 COM automation through PowerShell, Poppler `pdftoppm`, DOCX/OOXML ZIP inspection.

## Global Constraints

- Retained template: `C:\Users\czh\Desktop\1、工业和信息化部电子专利中心--技术交底书模板.docx`.
- Retained template SHA-256: `7C0563D0DE64B24E96651A254711A99CA9CEEA479F373121741575029C40DE0E`.
- Design authority: `docs/superpowers/specs/2026-07-29-k8s-dual-baseline-patent-disclosure-design.md`.
- Task directory: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730`.
- Final DOCX: `C:\Users\czh\Desktop\一种基于容器级双基线预测与分层门控的容器编排工作负载资源调配方法及系统--技术交底书.docx`.
- Bundled Python: `C:\Users\czh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`.
- Documents skill directory: `C:\Users\czh\.codex\plugins\cache\openai-primary-runtime\documents\26.727.11326\skills\documents`.
- Office MathML transform: `C:\Program Files\Microsoft Office\root\Office16\MML2OMML.XSL`.
- The retained template is never modified.
- The final document retains the template's original cover fields and Parts 1–6; it adds no independent “实施例” or other top-level section.
- No numeric worked example, simulated result, invented experiment, or unsupported performance percentage is included.
- Every mathematical expression is an editable OMML `m:oMath` or `m:oMathPara` object, never an image or plain-text pseudo-equation.
- Every prior-art item must have a verified title plus DOI, official documentation URL, or patent publication number.
- All six figures are black/white or grayscale, use Chinese labels, and do not carry formulas as pixels.
- Personal and applicant metadata not supplied by the user remain “待填写”.
- Final delivery contains only the requested DOCX; QA PDFs, PNGs, scripts, and evidence files remain temporary.

---

### Task 1: Distill and lock the retained template

**Files:**
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\artifact.md`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\template-style-evidence.json`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\template-package-inventory.json`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\render_with_word.ps1`

**Interfaces:**
- Consumes: retained template path and SHA-256 from Global Constraints.
- Produces: `artifact.md` with page system, typography, slots, package inventory, and fidelity gates used by Task 4.

- [ ] **Step 1: Create the task-local directory tree**

Run:

```powershell
New-Item -ItemType Directory -Force -Path `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730', `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\template-render', `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\final-render', `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\figures'
```

Expected: all four directories exist and the repository worktree is unchanged.

- [ ] **Step 2: Verify the retained template hash**

Run:

```powershell
(Get-FileHash -Algorithm SHA256 -LiteralPath `
  'C:\Users\czh\Desktop\1、工业和信息化部电子专利中心--技术交底书模板.docx').Hash
```

Expected: `7C0563D0DE64B24E96651A254711A99CA9CEEA479F373121741575029C40DE0E`.

- [ ] **Step 3: Run the packaged structural audits**

Run:

```powershell
$py='C:\Users\czh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$skill='C:\Users\czh\.codex\plugins\cache\openai-primary-runtime\documents\26.727.11326\skills\documents'
$doc='C:\Users\czh\Desktop\1、工业和信息化部电子专利中心--技术交底书模板.docx'
& $py "$skill\scripts\section_audit.py" $doc
& $py "$skill\scripts\style_lint.py" $doc --json `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\template-style-evidence.json'
& $py "$skill\scripts\heading_audit.py" $doc
& $py "$skill\scripts\images_audit.py" $doc
& $py "$skill\scripts\fields_report.py" $doc
& $py "$skill\scripts\content_controls.py" $doc list --json
```

Expected: one A4 portrait section, the retained header image, no heading styles, no fields, and no content controls.

- [ ] **Step 4: Render the template through Word when LibreOffice is unavailable**

Create `render_with_word.ps1` with a retry wrapper around these operations:

```powershell
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
$doc = $word.Documents.Open($InputDocx, $false, $true)
$doc.ExportAsFixedFormat($OutputPdf, 17)
$doc.Close(0)
$word.Quit()
```

The wrapper retries `RPC_E_CALL_REJECTED` up to 30 times at 500 ms intervals and always releases COM objects in `finally`.

Run the script against the retained template, then run:

```powershell
pdftoppm -png -r 150 `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\template.pdf' `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\template-render\page'
```

Expected: a PDF and one PNG per source page. If Word automation remains unavailable, record that limitation in `artifact.md` and continue with structural evidence as permitted by the documents skill.

- [ ] **Step 5: Write the exact template contract**

Write `artifact.md` with:

- reference path, SHA-256, page/section count, render and audit paths;
- A4 portrait geometry and measured margins;
- header image relationship and preservation rule;
- font, size, indentation, line spacing, and paragraph alignment for every distinct role;
- stable paragraph locators for title, identity fields, Parts 1–6, instructional paragraphs, and blank answer slots;
- package parts classified as editable or preserve-only;
- explicit permission to expand answer slots while preserving headings, instructional text, header, section geometry, and source styles;
- the fidelity rule that final pagination may increase but page geometry and recurring header remain unchanged.

Expected: every nonblank body paragraph, header object, and editable answer location is accounted for.

- [ ] **Step 6: Commit checkpoint**

No repository commit is made because all Task 1 outputs are temporary QA artifacts. Record completion in the plan checklist.

### Task 2: Build and verify the prior-art evidence set

**Files:**
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\source_evidence.md`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\source_evidence.json`

**Interfaces:**
- Consumes: the verified source requirements in the design spec.
- Produces: normalized source records with `kind`, `title`, `authors_or_applicant`, `year`, `identifier`, `url`, `verified_claims`, and `not_claimed`.

- [ ] **Step 1: Verify Kubernetes primary documentation**

Open and record:

- `https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/`
- `https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/`
- `https://kubernetes.io/docs/concepts/workloads/autoscaling/vertical-pod-autoscale/`

Record only the documented roles of Request, Limit, HPA, VPA, stabilization behavior, and container-level resource controls.

- [ ] **Step 2: Verify the two scholarly publications**

Open DOI landing pages and record exact metadata:

- Luciano Baresi, Davide Yi Xian Hu, Giovanni Quattrocchi, Luca Terracciano, “KOSMOS: Vertical and Horizontal Resource Autoscaling for Kubernetes”, 2021, DOI `10.1007/978-3-030-91431-8_59`.
- Zhiqiang Zhou et al., “AHPA: Adaptive Horizontal Pod Autoscaling Systems on Alibaba Cloud Container Service for Kubernetes”, Proceedings of the AAAI Conference on Artificial Intelligence 37(13), 2023, DOI `10.1609/aaai.v37i13.26852`.

For KOSMOS, record that it coordinates container-level vertical control and application-level horizontal control. For AHPA, record that it performs predictive horizontal Pod planning. Do not attribute the present invention's dual-baseline or direction-ledger mechanism to either source.

- [ ] **Step 3: Verify the two patent publications**

Open and record:

- `CN114389953B`, “一种基于流量预测的Kubernetes容器动态扩缩容方法及系统”.
- `CN115774605A`, “Kubernetes的预测式弹性伸缩方法及系统”.

Record publication number, title, applicant, priority/publication year, abstracted technical steps, and the exact distinction from the proposed invention. Do not describe a search portal's status label as a legal opinion.

- [ ] **Step 4: Write the normalized evidence files**

Use JSON records shaped as:

```json
{
  "kind": "paper",
  "title": "KOSMOS: Vertical and Horizontal Resource Autoscaling for Kubernetes",
  "authors_or_applicant": ["Luciano Baresi", "Davide Yi Xian Hu", "Giovanni Quattrocchi", "Luca Terracciano"],
  "year": 2021,
  "identifier": "10.1007/978-3-030-91431-8_59",
  "url": "https://doi.org/10.1007/978-3-030-91431-8_59",
  "verified_claims": ["coordinates container-level vertical and application-level horizontal scaling"],
  "not_claimed": ["does not establish the present invention's Request/Limit dual-baseline direction gate"]
}
```

Expected: every item is traceable to an official page, DOI page, or patent publication page.

- [ ] **Step 5: Evidence integrity check**

Run a script-free check that each DOI resolves and each patent publication number appears on its page. Reject any source whose title, identifier, or technical content cannot be verified.

- [ ] **Step 6: Commit checkpoint**

No repository commit is made because evidence files are temporary and the final source descriptions belong inside Part 2 of the DOCX.

### Task 3: Author the six template sections and native equation source

**Files:**
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\patent_content.json`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\equations.json`

**Interfaces:**
- Consumes: `artifact.md`, `source_evidence.json`, current project implementation, and the approved design spec.
- Produces: complete Chinese prose for Parts 1–6 plus MathML definitions consumed by Task 4.

- [ ] **Step 1: Author cover metadata without invention**

Set:

```json
{
  "title": "一种基于容器级双基线预测与分层门控的容器编排工作负载资源调配方法及系统",
  "inventors": "待填写",
  "first_inventor_id": "待填写",
  "writer": "待填写",
  "writer_phone": "待填写",
  "applicant": "待填写",
  "applicant_address_postcode": "待填写",
  "applicant_credit_code": "待填写"
}
```

- [ ] **Step 2: Author Parts 1–3**

Write full prose that:

- states the five technical problems from the design spec;
- explains Request, Limit, HPA, VPA, predictive autoscaling, KOSMOS, AHPA, `CN114389953B`, and `CN115774605A` only from verified evidence;
- derives the shortcomings through technical cause-and-effect;
- states the invention's corresponding objectives;
- avoids legal conclusions such as “具有新颖性” or unsupported claims that no prior art exists.

- [ ] **Step 3: Author Part 4 as a complete technical chain**

Use only subheadings inside Part 4 and cover:

- object and symbol definitions;
- controller ownership resolution;
- scope-aligned container aggregation;
- dual Request/Limit baseline formation;
- future-window forecasting and feature extraction;
- asymmetric scale-out/scale-in decisions;
- per-container Request/Limit targets;
- Workload replica target;
- total-capacity coordination;
- direction-consistency ledger;
- confidence, data-quality, history, cooldown, policy, and target-readiness gates;
- source-sensitive manual/confirmed/suggested target handling;
- command generation, execution result, and successful snapshot update;
- all specified degradation paths.

Do not add a numeric worked example, independent embodiment section, results section, or experiment section.

- [ ] **Step 4: Author Parts 5–6**

Part 5 lists the eight ordered protection points from the design spec. Part 6 explains only the five technically derived benefits and contains no percentage.

- [ ] **Step 5: Encode every equation as MathML**

Define MathML for at least these nine equation roles:

```text
limit_utilization
request_utilization
future_forecast
window_mean
quantile
trend_slope
replica_target
total_capacity
direction_rounds
execution_permission
```

Each MathML tree uses `<math xmlns="http://www.w3.org/1998/Math/MathML">` and structural elements such as `<mfrac>`, `<msub>`, `<msup>`, `<munderover>`, and `<mfenced>`. Each symbol is defined in adjacent Chinese prose.

- [ ] **Step 6: Content lint**

Reject content if any of these checks fail:

```text
Top-level sections exactly: 1, 2, 3, 4, 5, 6
Forbidden independent headings: 实施例, 实验结果, 验证报告, 权利要求书
Forbidden unsupported result pattern: 提升/降低/节省 + number + %
Required identifiers: 10.1007/978-3-030-91431-8_59, 10.1609/aaai.v37i13.26852, CN114389953B, CN115774605A
Required technical terms: Request, Limit, 容器级, 总容量, 动作方向, 数据质量, 冷却期
```

- [ ] **Step 7: Commit checkpoint**

No repository commit is made because the authored content is an intermediate for the requested DOCX.

### Task 4: Build six figures and the template-derived DOCX

**Files:**
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\build_patent_docx.py`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\figures\figure-1.png`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\figures\figure-2.png`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\figures\figure-3.png`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\figures\figure-4.png`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\figures\figure-5.png`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\figures\figure-6.png`
- Create: `C:\Users\czh\Desktop\一种基于容器级双基线预测与分层门控的容器编排工作负载资源调配方法及系统--技术交底书.docx`

**Interfaces:**
- Consumes: `artifact.md`, `patent_content.json`, `equations.json`, and the retained template.
- Produces: final DOCX with preserved template structure, six figures, and editable OMML equations.

- [ ] **Step 1: Implement monochrome diagram primitives**

In `build_patent_docx.py`, implement:

```python
from math import atan2, cos, pi, sin
from pathlib import Path


def _centered_multiline(draw, box, text, font):
    left, top, right, bottom = box
    bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=10, align="center")
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.multiline_text(
        ((left + right - width) / 2, (top + bottom - height) / 2),
        text,
        font=font,
        fill="black",
        spacing=10,
        align="center",
    )


def draw_box(draw, box, text, font, *, fill="white", outline="black", radius=12):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=4)
    _centered_multiline(draw, box, text, font)


def draw_arrow(draw, start, end, *, width=4, head=14):
    draw.line((start, end), fill="black", width=width)
    angle = atan2(end[1] - start[1], end[0] - start[0])
    points = [
        end,
        (
            end[0] - head * cos(angle - pi / 6),
            end[1] - head * sin(angle - pi / 6),
        ),
        (
            end[0] - head * cos(angle + pi / 6),
            end[1] - head * sin(angle + pi / 6),
        ),
    ]
    draw.polygon(points, fill="black")


def draw_diamond(draw, box, text, font, *, fill="white", outline="black"):
    left, top, right, bottom = box
    points = [
        ((left + right) / 2, top),
        (right, (top + bottom) / 2),
        ((left + right) / 2, bottom),
        (left, (top + bottom) / 2),
    ]
    draw.polygon(points, fill=fill, outline=outline)
    draw.line(points + [points[0]], fill=outline, width=4)
    _centered_multiline(draw, box, text, font)


def save_figure(image, path):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", dpi=(300, 300), optimize=True)
```

Use `C:\Windows\Fonts\msyh.ttc`, a white background, black strokes, grayscale fills, minimum 36 px labels, 1800 px page-width images, and at least 2200 px height when the flow requires it.

- [ ] **Step 2: Draw the six approved figures**

Create:

1. Overall architecture: monitoring source → scope alignment → dual-baseline forecast → coordinated target → gate ledger → executor.
2. Main method flow with failure/degradation branches.
3. Dual-baseline formation showing matched participating containers in numerator and denominator.
4. Vertical target plus replica target feeding total-capacity coordination.
5. Direction-ledger state transitions for same direction, reversal, hold, and insufficient data.
6. Gate and command sequence from recommendation through queueing, per-container resource command, replica command, and success snapshot.

Expected: no color-dependent meaning, no formula rasterization, no clipped Chinese labels.

- [ ] **Step 3: Implement MathML-to-OMML conversion**

Use:

```python
from lxml import etree

MML2OMML = etree.XSLT(
    etree.parse(r"C:\Program Files\Microsoft Office\root\Office16\MML2OMML.XSL")
)

def mathml_to_omml(mathml: str):
    node = etree.fromstring(mathml.encode("utf-8"))
    return MML2OMML(node).getroot()
```

Append the returned OMML element directly to a dedicated equation paragraph. Do not call `add_picture()` for equations.

- [ ] **Step 4: Implement template slot replacement**

Load a working copy of the retained DOCX with `python-docx`. Locate anchors by exact heading text from `artifact.md`; preserve each heading and instructional paragraph; insert authored answer paragraphs after the documented blank slot for that part. Reuse the source's Normal formatting and explicit Chinese font settings rather than applying a generic style pack.

- [ ] **Step 5: Insert Part 4 equations and figures**

For each formula:

- insert an explanatory paragraph;
- insert a centered OMML equation paragraph;
- insert symbol definitions immediately after the equation.

For each figure:

- insert a centered PNG at a width no greater than the template's usable page width;
- insert a centered caption using the approved exact title, for example `图1 系统总体架构图`;
- insert a full prose description that does not depend on viewing the figure.

- [ ] **Step 6: Preserve package-level template features**

After saving, compare the template and final package inventories. The header, header relationship, source image, section properties, theme, and numbering parts must remain present. Changes to `word/document.xml`, document relationships, media additions, and content types for the six figures are expected; unexplained removal of a preserve-only part is a failure.

- [ ] **Step 7: Run the builder**

Run:

```powershell
& 'C:\Users\czh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\build_patent_docx.py'
```

Expected: the final DOCX exists, opens as a ZIP package, retains the template header image, and contains six new body figures.

- [ ] **Step 8: Commit checkpoint**

No repository commit is made because the final artifact is delivered outside the repository and helper files are temporary.

### Task 5: Structural validation of the final DOCX

**Files:**
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\validate_patent_docx.py`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\validation-report.json`

**Interfaces:**
- Consumes: final DOCX, retained template, `source_evidence.json`, and `equations.json`.
- Produces: machine-readable pass/fail report used as the structural delivery gate.

- [ ] **Step 1: Write validator assertions**

The validator must assert:

```python
assert sha256(template_path) == EXPECTED_TEMPLATE_HASH
assert top_level_parts == ["1", "2", "3", "4", "5", "6"]
assert "实施例" not in independent_headings
assert omml_count >= 10
assert body_figure_count == 6
assert all(identifier in full_text for identifier in REQUIRED_IDENTIFIERS)
assert not unsupported_percentage_pattern.search(full_text)
assert preserved_header_image
assert section_count == 1
assert page_width_and_height_match_template
```

It must also check that equation paragraphs contain `m:oMath`/`m:oMathPara` and do not contain `w:drawing`.

- [ ] **Step 2: Run structural audits**

Run:

```powershell
$py='C:\Users\czh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$skill='C:\Users\czh\.codex\plugins\cache\openai-primary-runtime\documents\26.727.11326\skills\documents'
$doc='C:\Users\czh\Desktop\一种基于容器级双基线预测与分层门控的容器编排工作负载资源调配方法及系统--技术交底书.docx'
& $py 'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\validate_patent_docx.py'
& $py "$skill\scripts\section_audit.py" $doc
& $py "$skill\scripts\style_lint.py" $doc --json `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\final-style-evidence.json'
& $py "$skill\scripts\images_audit.py" $doc
& $py "$skill\scripts\fields_report.py" $doc
```

Expected: validator PASS, one section, seven total images including the retained header image, at least ten OMML equations, and no unexpected fields.

- [ ] **Step 3: Inspect formula OOXML**

Open `word/document.xml` read-only and inspect every `m:oMath` tree for fractions, scripts, sums, and operators. Confirm the visible equation text is not duplicated as a fallback plain-text equation.

- [ ] **Step 4: Repair and rerun on any failure**

Fix the builder or content source, rebuild the DOCX, and rerun all Task 5 checks. Do not patch the final DOCX manually in Word because the result must remain reproducible.

- [ ] **Step 5: Commit checkpoint**

No repository commit is made because validation outputs are temporary.

### Task 6: Render every page and perform visual QA

**Files:**
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\final.pdf`
- Create: `C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\final-render\page-*.png`

**Interfaces:**
- Consumes: structurally valid final DOCX and `render_with_word.ps1`.
- Produces: page images used for the final visual delivery gate.

- [ ] **Step 1: Attempt the packaged renderer first**

Run:

```powershell
& 'C:\Users\czh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  'C:\Users\czh\.codex\plugins\cache\openai-primary-runtime\documents\26.727.11326\skills\documents\render_docx.py' `
  'C:\Users\czh\Desktop\一种基于容器级双基线预测与分层门控的容器编排工作负载资源调配方法及系统--技术交底书.docx' `
  --output_dir 'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\final-render' `
  --emit_pdf
```

Expected on a machine with LibreOffice: one PNG per page. If the known missing `soffice` error recurs, use Step 2.

- [ ] **Step 2: Render through Microsoft Word and Poppler**

Run `render_with_word.ps1` against the final DOCX, exporting `final.pdf`, then:

```powershell
pdftoppm -png -r 180 `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\final.pdf' `
  'C:\Users\czh\AppData\Local\Temp\codex-patent-disclosure-20260730\final-render\page'
```

Expected: one PNG per PDF page.

- [ ] **Step 3: Inspect every page at full detail**

Use the local image viewer on every PNG. Check:

- template header placement and first-page title block;
- no clipped or overlapping Chinese text;
- no mojibake or missing glyphs;
- equations render as built-up Word equations with intact fractions, indices, and sums;
- figures are readable, monochrome, centered, and not split incorrectly;
- captions stay with figures;
- headings do not orphan at page bottoms;
- no unexpected blank pages or large layout gaps;
- Parts 1–6 remain visually recognizable as the retained template.

- [ ] **Step 4: Iterate until flawless**

For any defect, change only `patent_content.json`, `equations.json`, figure layout, or `build_patent_docx.py`; rebuild; rerun Task 5; rerender into a new QA directory; inspect every page again.

- [ ] **Step 5: Record visual gate**

Write `visual-qa.md` with page count, inspected page filenames, and a concise pass statement. If neither LibreOffice nor Word rendering succeeds, record the exact limitation and do not claim visual QA passed.

### Task 7: Final integrity and delivery

**Files:**
- Verify: `C:\Users\czh\Desktop\一种基于容器级双基线预测与分层门控的容器编排工作负载资源调配方法及系统--技术交底书.docx`

**Interfaces:**
- Consumes: structurally and visually approved final DOCX.
- Produces: the single user-facing deliverable.

- [ ] **Step 1: Reverify the retained template**

Expected SHA-256 remains `7C0563D0DE64B24E96651A254711A99CA9CEEA479F373121741575029C40DE0E`.

- [ ] **Step 2: Reverify all citations**

Compare Part 2 against `source_evidence.json`. Every title, author/applicant, year, DOI, and patent publication number must match the verified source.

- [ ] **Step 3: Run the final structural and visual gates**

Rerun `validate_patent_docx.py`, confirm `validation-report.json` is PASS, and confirm the latest `visual-qa.md` covers every latest-render page.

- [ ] **Step 4: Confirm deliverable discipline**

Confirm the final response links only the DOCX. Do not deliver the task scripts, PDF, page PNGs, evidence files, or `artifact.md` unless requested.

- [ ] **Step 5: Deliver**

Report that the document follows the retained six-part template, contains native editable Word equations, includes six monochrome figures, and uses verified literature and patent publications. Include the required document output citation exactly once.
