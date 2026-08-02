const DEFAULT_TEMPLATE_NAME = "TS26/VIZITKA_TEMPLATE";
const DEFAULT_OUTPUT_PREFIX = "TS26 CARD / ";
const NBSP = "\u00a0";

figma.showUI(__html__, { width: 460, height: 680, themeColors: true });

function normalize(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function normalizeRussian(value) {
  const text = normalize(value);
  if (!text) return "";
  return text.replace(/(^|\s)(а|и|в|во|на|но|из|за|по|к|ко|с|со|у|о|об|от|до|для|не|ни)\s+(?=\S)/giu, `$1$2${NBSP}`);
}

function stringHash(text) {
  let hash = 2166136261;
  const value = String(text || "");
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16);
}

function googleSheetExportUrl(url) {
  const text = normalize(url);
  const match = text.match(/docs\.google\.com\/spreadsheets\/d\/([^/]+)/);
  if (!match) return text;
  let gid = "0";
  const gidMatch = text.match(/[?#&]gid=(\d+)/);
  if (gidMatch) gid = gidMatch[1];
  if (/\/export\?/.test(text) && /[?&]format=tsv\b/.test(text)) return text;
  return `https://docs.google.com/spreadsheets/d/${match[1]}/export?format=tsv&gid=${gid}`;
}

function parseTsv(text) {
  const lines = String(text || "").replace(/\r/g, "").split("\n").filter(Boolean);
  if (!lines.length) return [];
  const headers = lines.shift().split("\t");
  return lines.map(line => {
    const cells = line.split("\t");
    return headers.reduce((row, key, index) => {
      row[key] = cells[index] || "";
      return row;
    }, {});
  });
}

function validation(row) {
  const warnings = [];
  const name = normalize(row["ФИО спикера"]);
  const position = normalize(row["Должность"]);
  if (!name || name.split(" ").length < 2) warnings.push("проверьте ФИО");
  if (!position) warnings.push("нет должности");
  if (/\d+\)|^\d+\./.test(name) || /\d+\)|^\d+\./.test(position)) warnings.push("осталась нумерация");
  if (position.length > 180) warnings.push("должность длиннее 180 символов");
  return warnings;
}

async function fetchText(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Источник вернул HTTP ${response.status}.`);
  return response.text();
}

async function previewRows(message) {
  let text = String(message.tsv || "").trim();
  let sourceUrl = "";
  if (!text) {
    const url = googleSheetExportUrl(message.url);
    if (!url) throw new Error("Укажите TSV-ссылку или вставьте данные.");
    sourceUrl = url;
    text = await fetchText(url);
  }
  const rows = parseTsv(text);
  if (!rows.length) throw new Error("В TSV нет строк.");
  return {
    sourceUrl,
    sourceHash: stringHash(text),
    rows: rows.map((row, index) => ({
      row,
      index: index + 1,
      warnings: validation(row),
    })),
  };
}

async function loadImages(rows) {
  const images = {};
  const warnings = [];
  for (const row of rows) {
    const url = normalize(row["Фото на плашку"]);
    const personId = normalize(row.person_id) || normalize(row["ФИО спикера"]).replace(/\s+/g, "_");
    if (!url || !personId) continue;
    try {
      const response = await fetch(url);
      if (!response.ok) {
        warnings.push(`${row["ФИО спикера"] || personId}: фото вернуло HTTP ${response.status}`);
        continue;
      }
      images[personId] = Array.from(new Uint8Array(await response.arrayBuffer()));
    } catch (error) {
      warnings.push(`${row["ФИО спикера"] || personId}: фото не загрузилось (${error.message || error})`);
    }
  }
  return { images, warnings };
}

function textNodes(root) {
  return root.findAll(node => node.type === "TEXT");
}

function findNamed(root, names) {
  const wanted = new Set(names.map(item => item.toLowerCase()));
  return root.findOne(node => wanted.has(String(node.name || "").toLowerCase()));
}

function templateNode(templateName) {
  return figma.currentPage.findOne(node => node.name === templateName) ||
    figma.currentPage.findOne(node => node.name === DEFAULT_TEMPLATE_NAME);
}

function cloneTemplate(template) {
  if (template.type === "COMPONENT") return template.createInstance();
  return template.clone();
}

async function setText(node, value) {
  if (!node || node.type !== "TEXT") return false;
  const font = node.fontName;
  if (font && font !== figma.mixed) await figma.loadFontAsync(font);
  node.characters = value;
  return true;
}

function setPhoto(root, imageHash) {
  if (!imageHash) return false;
  const photo = findNamed(root, ["PHOTO", "ФОТО", "PHOTO_FRAME"]);
  if (!photo || !("fills" in photo)) return false;
  photo.fills = [{ type: "IMAGE", imageHash, scaleMode: "FILL" }];
  return true;
}

async function fillCard(card, row, imageBytes) {
  const name = normalize(row["ФИО спикера"]);
  const position = normalizeRussian(row["Должность"]);
  const nodes = textNodes(card);
  const nameNode = findNamed(card, ["FIO", "NAME", "ФИО", "ИМЯ", "ФИО СПИКЕРА"]);
  const positionNode = findNamed(card, ["POSITION", "DOLZHNOST", "ДОЛЖНОСТЬ", "ДОЛЖНОСТЬ СПИКЕРА"]);
  const unnamed = nodes.filter(node => node !== nameNode && node !== positionNode);
  await setText(nameNode || nodes[0], name);
  await setText(positionNode || unnamed[0], position);
  let photo = false;
  if (imageBytes && imageBytes.length) {
    const image = figma.createImage(new Uint8Array(imageBytes));
    photo = setPhoto(card, image.hash);
  }
  return { photo, position, warnings: validation(row) };
}

function placeCard(card, index, gap) {
  const generated = figma.currentPage.findAll(node => String(node.name || "").startsWith(DEFAULT_OUTPUT_PREFIX));
  const last = generated[generated.length - 1];
  card.x = last ? last.x + last.width + gap : index * (card.width + gap);
  card.y = last ? last.y : 0;
}

async function generate(rows, settings) {
  const template = templateNode(settings.templateName || DEFAULT_TEMPLATE_NAME);
  if (!template) throw new Error(`Не найден шаблон «${settings.templateName || DEFAULT_TEMPLATE_NAME}» на текущей странице.`);
  const results = [];
  const generatedNames = [];
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    const personId = normalize(row.person_id) || normalize(row["ФИО спикера"]).replace(/\s+/g, "_");
    const generatedName = `${settings.outputPrefix || DEFAULT_OUTPUT_PREFIX}${personId}`;
    let card = figma.currentPage.findOne(node => node.name === generatedName);
    const created = !card;
    if (!card) card = cloneTemplate(template);
    card.name = generatedName;
    if (created) placeCard(card, index, Number(settings.gap) || 80);
    const imageBytes = settings.images && settings.images[personId];
    const result = await fillCard(card, row, imageBytes);
    generatedNames.push(generatedName);
    results.push({ name: row["ФИО спикера"], created, photo: result.photo, warnings: result.warnings });
  }
  figma.currentPage.selection = figma.currentPage.findAll(node => generatedNames.includes(node.name));
  figma.viewport.scrollAndZoomIntoView(figma.currentPage.selection);
  return results;
}

figma.ui.onmessage = async message => {
  try {
    if (message.type === "preview") {
      const result = await previewRows(message);
      figma.ui.postMessage({ type: "preview-done", result });
      return;
    }
    if (message.type === "generate") {
      const rows = message.rows || [];
      const loaded = await loadImages(rows);
      const result = await generate(rows, { ...(message.settings || {}), images: loaded.images });
      figma.ui.postMessage({ type: "done", result, imageWarnings: loaded.warnings });
    }
  } catch (error) {
    figma.ui.postMessage({ type: "error", message: error.message || String(error) });
  }
};
