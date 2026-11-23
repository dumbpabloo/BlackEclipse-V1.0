// ── Response export ──────────────────────────────────────────────────────────
// JSON / CSV / PNG / PDF export for a validated response, plus the base64 helper
// used to persist a response to the Artifact Center. Heavy libraries (jsPDF,
// autotable) are dynamically imported so they never enter the chat bundle.

import type { ParsedResponse, TableCell, ValidatedBlock } from './contract';
import { finalizeParsed } from './finalize';
import { toMarkdownProjection } from './projection';
import { fetchChartPng } from './chart-registry';
import { inlineText, parseMarkdown } from '@/lib/chat/markdown';
import { saveBlob } from '@/lib/desktop/download';

export function downloadBlob(filename: string, blob: Blob): void {
  // One download path for the whole app: the desktop shell writes the file (and
  // reveals it); a browser keeps the anchor download.
  saveBlob(filename, blob);
}

export function toBase64Utf8(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = '';
  bytes.forEach((b) => {
    binary += String.fromCharCode(b);
  });
  return btoa(binary);
}

/** A blob as a data URL — the shape jsPDF embeds. */
async function blobToDataUrl(blob: Blob): Promise<string> {
  const buffer = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  buffer.forEach((b) => {
    binary += String.fromCharCode(b);
  });
  return `data:${blob.type || 'image/png'};base64,${btoa(binary)}`;
}

/**
 * A PNG's width/height ratio, read from its IHDR header.
 *
 * The chart engine draws a transparent figure sized to the block, so the PDF
 * embeds it at the picture's OWN ratio rather than a guessed one. Returns null
 * for a blob that is not a PNG — the caller then falls back to a default.
 */
async function pngAspectRatio(blob: Blob): Promise<number | null> {
  try {
    const head = new Uint8Array(await blob.slice(0, 24).arrayBuffer());
    if (head.length < 24) return null;
    const view = new DataView(head.buffer);
    const width = view.getUint32(16);
    const height = view.getUint32(20);
    if (!width || !height) return null;
    return width / height;
  } catch {
    return null;
  }
}

function safeName(name: string, ext: string): string {
  const base = name.replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'eclipse-response';
  return `${base}.${ext}`;
}

export function responseToJson(response: ParsedResponse): string {
  // Finalize defensively: an export must never contain a rejected (`unknown`)
  // block or an error diagnostic, regardless of the caller.
  return JSON.stringify(finalizeParsed(response).response, null, 2);
}

export function downloadResponseJson(response: ParsedResponse, name: string): void {
  downloadBlob(safeName(name, 'json'), new Blob([responseToJson(response)], { type: 'application/json' }));
}

/** The deterministic Markdown projection — the same text any non-block surface shows. */
export function responseToMarkdown(response: ParsedResponse): string {
  return toMarkdownProjection(response);
}

export function downloadResponseMarkdown(response: ParsedResponse, name: string): void {
  downloadBlob(safeName(name, 'md'), new Blob([responseToMarkdown(response)], { type: 'text/markdown' }));
}

function cellToText(cell: TableCell | undefined): string {
  if (cell == null) return '';
  if (typeof cell === 'boolean') return cell ? 'yes' : 'no';
  return String(cell);
}

export function tableToCsv(
  columns: { key: string; label: string }[],
  rows: Record<string, TableCell>[],
): string {
  const escape = (value: string) => (/[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value);
  const header = columns.map((c) => escape(c.label)).join(',');
  const body = rows.map((row) => columns.map((c) => escape(cellToText(row[c.key]))).join(',')).join('\n');
  return `${header}\n${body}`;
}

export function downloadTableCsv(
  columns: { key: string; label: string }[],
  rows: Record<string, TableCell>[],
  name: string,
): void {
  downloadBlob(safeName(name, 'csv'), new Blob([tableToCsv(columns, rows)], { type: 'text/csv' }));
}

/**
 * Download a chat chart as a PNG.
 *
 * Either engine is supported: the Python engine's picture is an ARTIFACT
 * (fetched back from the Artifact Center), and the ECharts path is a live
 * canvas (captured with `getDataURL`). Whichever engine drew the chart, the
 * export is the picture the user is looking at.
 */
export async function downloadChartPng(chartId: string, name: string): Promise<boolean> {
  // A chat chart is drawn by the chart engine, so its picture IS an artifact:
  // the bytes are fetched back from the Artifact Center.
  const artifact = await fetchChartPng(chartId);
  if (!artifact) return false;
  downloadBlob(safeName(name, 'png'), artifact.blob);
  return true;
}

/** Flatten a Markdown string to plain text lines for PDF body text. */
export function markdownToPlainText(markdown: string): string {
  const blocks = parseMarkdown(markdown);
  const blockText = (block: (typeof blocks)[number]): string => {
    switch (block.type) {
      case 'paragraph':
        return inlineText(block.children);
      case 'heading':
        return inlineText(block.children).toUpperCase();
      case 'code':
        return block.value;
      case 'math':
        // A formula has no plain-text form; its source is the honest one.
        return block.value;
      case 'blockquote':
        return block.children.map(blockText).join('\n');
      case 'list':
        return block.items.map((item) => `- ${inlineText(item)}`).join('\n');
      case 'table':
        return [block.header, ...block.rows].map((cells) => cells.map(inlineText).join(' | ')).join('\n');
      case 'hr':
        return '--------------------------------';
    }
  };
  return blocks.map(blockText).join('\n');
}

interface PdfTableRow {
  head: string[];
  body: string[][];
}

function tableStateFor(block: ValidatedBlock): PdfTableRow | null {
  if (block.type === 'data_table') {
    return {
      head: block.columns.map((c) => c.label),
      body: block.rows.map((row) => block.columns.map((c) => cellToText(row[c.key]))),
    };
  }
  if (block.type === 'metric_grid') {
    return {
      head: ['Metric', 'Value', 'Delta'],
      body: block.metrics.map((m) => [
        m.label,
        m.unavailable ? 'UNAVAILABLE' : `${m.prefix ?? ''}${String(m.value)}${m.unit ?? ''}`,
        m.delta != null ? String(m.delta) : '',
      ]),
    };
  }
  if (block.type === 'key_value') {
    return { head: ['Field', 'Value'], body: block.items.map((i) => [i.label, String(i.value)]) };
  }
  if (block.type === 'citations') {
    return {
      head: ['#', 'Source', 'URL'],
      body: block.sources.map((s, i) => [String(i + 1), s.title, s.url ?? '']),
    };
  }
  if (block.type === 'timeline') {
    return { head: ['Time', 'Event', 'Detail'], body: block.events.map((e) => [e.time, e.title, e.description ?? '']) };
  }
  if (block.type === 'stat_hero') {
    return {
      head: ['Statistic', 'Value', 'Delta'],
      body: block.stats.map((s) => [
        s.label,
        s.unavailable ? 'UNAVAILABLE' : `${s.prefix ?? ''}${String(s.value)}${s.unit ?? ''}`,
        s.delta != null ? String(s.delta) : '',
      ]),
    };
  }
  if (block.type === 'gauge') {
    return {
      head: ['Gauge', 'Value', 'Max'],
      body: block.gauges.map((g) => [g.label, String(g.value), `${g.max}${g.unit ? ` ${g.unit}` : ''}`]),
    };
  }
  if (block.type === 'ranked_list') {
    return {
      head: ['#', 'Item', block.valueLabel ?? 'Value'],
      body: block.items.map((item, i) => [String(i + 1), item.label, item.display ?? `${item.value}${item.unit ? ` ${item.unit}` : ''}`]),
    };
  }
  if (block.type === 'comparison_grid') {
    return {
      head: ['Attribute', ...block.columns.map((c) => c.label)],
      body: block.rows.map((row) => [
        row.label,
        ...block.columns.map((_, c) => cellToText(row.values[c] ?? null)),
      ]),
    };
  }
  return null;
}

function accentColor(): [number, number, number] {
  return [46, 160, 67];
}

/**
 * Build a PDF of a response. Charts that are currently mounted are embedded as
 * images; text, tables, metrics, key-value panels, citations and timelines are
 * rendered as text/autotables. Blocks with no PDF representation are listed in
 * an appendix note rather than silently dropped.
 */
export async function responseToPdf(response: ParsedResponse): Promise<Blob> {
  // Finalize first so the PDF is built from repaired/degraded blocks — never a
  // rejected block or an error note.
  const safe = finalizeParsed(response).response;
  const { default: JsPDF } = await import('jspdf');
  const { autoTable } = await import('jspdf-autotable');
  const doc = new JsPDF({ unit: 'pt', format: 'a4' });
  const margin = 40;
  const pageWidth = doc.internal.pageSize.getWidth();
  const contentWidth = pageWidth - margin * 2;
  let y = margin;

  const ensureSpace = (needed: number) => {
    if (y + needed > doc.internal.pageSize.getHeight() - margin) {
      doc.addPage();
      y = margin;
    }
  };

  const heading = (text: string, size: number) => {
    ensureSpace(size + 8);
    doc.setFontSize(size);
    doc.setTextColor(20, 20, 20);
    doc.text(text, margin, y);
    y += size + 6;
  };

  const paragraph = (text: string) => {
    doc.setFontSize(10);
    doc.setTextColor(40, 40, 40);
    const lines = doc.splitTextToSize(text || ' ', contentWidth);
    lines.forEach((line: string) => {
      ensureSpace(14);
      doc.text(line, margin, y);
      y += 14;
    });
  };

  heading(safe.title ?? 'Eclipse Response', 16);
  if (safe.summary) paragraph(safe.summary);
  y += 4;

  const missing: string[] = [];

  for (const block of safe.blocks) {
    switch (block.type) {
      case 'heading':
        heading(block.text, 13);
        break;
      case 'markdown':
        paragraph(markdownToPlainText(block.markdown));
        y += 6;
        break;
      case 'callout':
        heading(block.title ?? block.tone.toUpperCase(), 11);
        paragraph(markdownToPlainText(block.markdown));
        y += 6;
        break;
      case 'details':
        heading(block.summary, 11);
        paragraph(markdownToPlainText(block.markdown));
        y += 6;
        break;
      case 'metric_grid':
      case 'data_table':
      case 'key_value':
      case 'citations':
      case 'timeline':
      case 'stat_hero':
      case 'gauge':
      case 'ranked_list':
      case 'comparison_grid': {
        const table = tableStateFor(block) as PdfTableRow;
        if (block.type === 'metric_grid' && block.title) heading(block.title, 12);
        if (block.type === 'data_table' && block.title) heading(block.title, 12);
        if (block.type === 'citations') heading(block.title ?? 'Sources', 12);
        if (block.type === 'timeline' && block.title) heading(block.title, 12);
        if (block.type === 'stat_hero' && block.title) heading(block.title, 12);
        if (block.type === 'gauge' && block.title) heading(block.title, 12);
        if (block.type === 'ranked_list' && block.title) heading(block.title, 12);
        if (block.type === 'comparison_grid' && block.title) heading(block.title, 12);
        autoTable(doc, {
          head: [table.head],
          body: table.body,
          startY: y,
          margin: { left: margin, right: margin },
          styles: { fontSize: 9, cellPadding: 4 },
          headStyles: { fillColor: accentColor(), textColor: 255 },
          theme: 'striped',
        });
        y = ((doc as unknown as { lastAutoTable?: { finalY: number } }).lastAutoTable?.finalY ?? y) + 16;
        break;
      }
      case 'chart': {
        if (block.title) heading(block.title, 12);
        // The picture is an artifact: its bytes are fetched back from the
        // Artifact Center, so the PDF embeds exactly what the user saw.
        // Either engine: the drawn picture is an artifact, the interactive
        // chart is a live canvas.
        const artifact = block.id ? await fetchChartPng(block.id) : null;
        if (artifact) {
          try {
            const dataUrl = await blobToDataUrl(artifact.blob);
            const imgWidth = contentWidth;
            const ratio = (await pngAspectRatio(artifact.blob)) ?? 0.5;
            const imgHeight = Math.min(imgWidth * ratio, 460);
            ensureSpace(imgHeight + 12);
            // The engine draws a TRANSPARENT figure with light ink (it lives on
            // the app's dark glass), so the PDF puts it on the app's own dark
            // panel — otherwise the picture would be light ink on a white page.
            doc.setFillColor(18, 18, 20);
            doc.rect(margin, y, imgWidth, imgHeight, 'F');
            doc.addImage(dataUrl, 'PNG', margin, y, imgWidth, imgHeight);
            y += imgHeight + 14;
          } catch {
            missing.push(`chart (${block.title ?? 'untitled'}) could not be captured`);
          }
        } else {
          missing.push(`chart (${block.title ?? 'untitled'}) not drawn; not embedded`);
        }
        break;
      }
      case 'financial_chart':
        missing.push(`financial chart for ${block.symbol} (interactive; not embedded in PDF)`);
        break;
      case 'diagram':
        if (block.title) heading(block.title, 12);
        paragraph(`Diagram (${block.diagramType}) with ${block.nodes.length} nodes and ${block.edges.length} edges.`);
        paragraph(block.nodes.map((n) => n.label).join(' -> '));
        y += 6;
        break;
      case 'mermaid':
        if (block.title) heading(block.title, 12);
        paragraph('Mermaid diagram — see the app for the rendered figure.');
        y += 6;
        break;
      case 'tabs':
        missing.push('tabbed content is flattened in PDF; open the app for tabs');
        break;
      case 'controls':
        missing.push('interactive controls are omitted from PDF');
        break;
      case 'stepper':
        if (block.title) heading(block.title, 12);
        block.steps.forEach((step, i) => {
          paragraph(`${i + 1}. ${step.title} (${step.status})`);
          if (step.description) paragraph(`   ${markdownToPlainText(step.description)}`);
        });
        y += 4;
        break;
      case 'infographic':
        if (block.title) heading(block.title, 12);
        autoTable(doc, {
          head: [['Card', 'Value', 'Detail']],
          body: block.cards.map((c) => [
            c.label,
            c.unavailable ? 'UNAVAILABLE' : `${String(c.value ?? '')}${c.unit ?? ''}`,
            c.description ?? '',
          ]),
          startY: y,
          margin: { left: margin, right: margin },
          styles: { fontSize: 9, cellPadding: 4 },
          headStyles: { fillColor: accentColor(), textColor: 255 },
          theme: 'striped',
        });
        y = ((doc as unknown as { lastAutoTable?: { finalY: number } }).lastAutoTable?.finalY ?? y) + 16;
        break;
      case 'report':
        if (block.title) heading(block.title, 16);
        if (block.summary) paragraph(markdownToPlainText(block.summary));
        block.sections.forEach((section) => {
          if (section.heading) heading(section.heading, 12);
          if (section.markdown) paragraph(markdownToPlainText(section.markdown));
          if (section.blocks && section.blocks.length) {
            missing.push('a report section contains nested blocks; open the app for the full render');
          }
          y += 4;
        });
        break;
      case 'node_graph':
        if (block.title) heading(block.title, 12);
        paragraph(`Interactive graph with ${block.nodes.length} nodes and ${block.edges.length} edges.`);
        block.nodes.forEach((n) => {
          paragraph(`- ${n.label}${n.group ? ` (${n.group})` : ''}${n.detail ? `: ${n.detail}` : ''}`);
        });
        y += 6;
        break;
      case 'action_group':
        if (block.title) heading(block.title, 12);
        paragraph('Proposed next steps:');
        block.actions.forEach((action) => paragraph(`- ${action.label}${action.description ? ` — ${action.description}` : ''}`));
        y += 6;
        break;
      case 'artifact':
        if (block.title) heading(block.title, 12);
        autoTable(doc, {
          head: [['Artifact', 'Category', 'Size']],
          body: block.artifacts.map((a) => [a.name, a.category ?? '', a.sizeBytes != null ? `${a.sizeBytes} B` : '']),
          startY: y,
          margin: { left: margin, right: margin },
          styles: { fontSize: 9, cellPadding: 4 },
          headStyles: { fillColor: accentColor(), textColor: 255 },
          theme: 'striped',
        });
        y = ((doc as unknown as { lastAutoTable?: { finalY: number } }).lastAutoTable?.finalY ?? y) + 16;
        break;
      case 'badge_row': {
        if (block.title) heading(block.title, 12);
        paragraph(block.badges.map((b) => b.label).join(' · '));
        y += 6;
        break;
      }
      case 'carousel':
        if (block.title) heading(block.title, 12);
        block.slides.forEach((slide, i) => {
          paragraph(`${i + 1}. ${slide.label ?? slide.title ?? `Slide ${i + 1}`}`);
          if (slide.blocks && slide.blocks.length) {
            missing.push(`carousel slide ${i + 1} contains nested blocks; open the app for the full render`);
          }
        });
        y += 4;
        break;
      case 'anatomy':
        if (block.title) heading(block.title, 12);
        paragraph(`${block.parts.length} parts, ${block.wires.length} connections`);
        block.stages.forEach((s, i) => paragraph(`${i + 1}. ${s.label}${s.readout ? ` — ${s.readout}` : ''}`));
        y += 4;
        break;
      case 'model3d':
        if (block.title) heading(block.title, 12);
        paragraph(`${block.parts.length} parts · true 3D model (open the app to orbit or export GLB)`);
        block.stages.forEach((s, i) => paragraph(`${i + 1}. ${s.label}${s.readout ? ` — ${s.readout}` : ''}`));
        y += 4;
        break;
      case 'interactive':
        if (block.title) heading(block.title, 12);
        paragraph(
          block.dataset
            ? `Interactive app driven by ${block.dataset.symbol.toUpperCase()} (${block.dataset.range ?? '6mo'}, ${block.dataset.interval ?? '1d'}). Open the app to use it.`
            : 'Interactive app. Open the app to use it.',
        );
        y += 4;
        break;
      default:
        missing.push(`block "${(block as { type: string }).type}" has no PDF representation`);
    }
  }

  if (missing.length) {
    heading('Export notes', 11);
    missing.forEach((note) => paragraph(`- ${note}`));
  }

  return doc.output('blob');
}

export async function downloadResponsePdf(response: ParsedResponse, name: string): Promise<void> {
  const blob = await responseToPdf(response);
  downloadBlob(safeName(name, 'pdf'), blob);
}
