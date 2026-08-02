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
    results.push({ name: row["ФИО спикера"], created, photo: result.photo, warnings: result.warnings });
  }
  figma.currentPage.selection = figma.currentPage.findAll(node => results.some(item => node.name === `${settings.outputPrefix || DEFAULT_OUTPUT_PREFIX}${normalize(item.name).replace(/\s+/g, "_")}`));
  figma.viewport.scrollAndZoomIntoView(figma.currentPage.selection);
  return results;
}

figma.ui.onmessage = async message => {
  if (message.type !== "generate") return;
  try {
    const result = await generate(message.rows || [], message.settings || {});
    figma.ui.postMessage({ type: "done", result });
  } catch (error) {
    figma.ui.postMessage({ type: "error", message: error.message || String(error) });
  }
};
