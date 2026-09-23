// Lightweight Windows Script Host collector for the residential Off1 machine.
// It intentionally uses only built-in Windows COM components: no Python,
// browser automation, visible console window, or third-party dependencies.

var fso = new ActiveXObject("Scripting.FileSystemObject");
var shell = new ActiveXObject("WScript.Shell");
var baseDir = fso.GetParentFolderName(WScript.ScriptFullName);
var configPath = fso.BuildPath(baseDir, "collector-config.json");
var logPath = fso.BuildPath(baseDir, "collector.log");
var seenPath = fso.BuildPath(baseDir, "seen.json");

// Windows Script Host still ships the legacy JScript engine on some PCs.
// Supply the two ES5 helpers the collector needs instead of silently exiting
// on machines where JSON and Date#toISOString are absent.
if (typeof JSON === "undefined") JSON = {};
if (typeof JSON.parse !== "function") {
  JSON.parse = function (value) { return eval("(" + value + ")"); };
}
if (typeof JSON.stringify !== "function") {
  JSON.stringify = function (value) {
    if (value === null) return "null";
    var kind = typeof value;
    if (kind === "string") return '"' + value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\r/g, "\\r").replace(/\n/g, "\\n") + '"';
    if (kind === "number" || kind === "boolean") return String(value);
    var parts = [], i, key;
    if (value instanceof Array) {
      for (i = 0; i < value.length; i++) parts.push(JSON.stringify(value[i]));
      return "[" + parts.join(",") + "]";
    }
    for (key in value) if (value.hasOwnProperty(key)) {
      parts.push(JSON.stringify(key) + ":" + JSON.stringify(value[key]));
    }
    return "{" + parts.join(",") + "}";
  };
}

function readText(path) {
  var stream = fso.OpenTextFile(path, 1, false, -1);
  var value = stream.ReadAll();
  stream.Close();
  return value;
}

function writeText(path, value) {
  var stream = fso.OpenTextFile(path, 2, true, -1);
  stream.Write(value);
  stream.Close();
}

function appendLog(message) {
  try {
    var stream = fso.OpenTextFile(logPath, 8, true, -1);
    stream.WriteLine(message);
    stream.Close();
  } catch (_) {}
}

function request(method, url, body, headers) {
  var http = new ActiveXObject("MSXML2.ServerXMLHTTP.6.0");
  http.setTimeouts(15000, 15000, 30000, 30000);
  http.open(method, url, false);
  http.setRequestHeader("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36");
  http.setRequestHeader("Accept", "application/json,text/plain,*/*");
  http.setRequestHeader("Accept-Language", "ru-RU,ru;q=0.9");
  http.setRequestHeader("device", "pc");
  http.setRequestHeader("language", "ru_RU");
  http.setRequestHeader("country-id", "12");
  http.setRequestHeader("user-hash", config.userHash);
  http.setRequestHeader("request-id", createGuid());
  http.setRequestHeader("Origin", "https://lalafo.kg");
  http.setRequestHeader("Referer", "https://lalafo.kg/");
  if (headers) {
    for (var name in headers) http.setRequestHeader(name, headers[name]);
  }
  http.send(body || null);
  if (http.status < 200 || http.status >= 300) {
    throw new Error("HTTP " + http.status + " for " + url);
  }
  return http.responseText;
}

function createGuid() {
  return String(new Date().getTime()) + "-" +
    String(Math.floor(Math.random() * 1000000000)) + "-" +
    String(Math.floor(Math.random() * 1000000000));
}

function safeJson(path, fallback) {
  try { return JSON.parse(readText(path)); } catch (_) { return fallback; }
}

function normalized(value) {
  return String(value || "").toLowerCase().replace(/ё/g, "е");
}

function paramsMap(items) {
  var result = {};
  items = items || [];
  for (var i = 0; i < items.length; i++) {
    result[String(items[i].name || "")] = items[i].value;
  }
  return result;
}

function photoUrls(raw) {
  var result = [];
  var images = raw.images || [];
  for (var i = 0; i < images.length; i++) {
    var url = images[i].original_url || images[i].original_webp_url;
    if (url) result.push(String(url));
  }
  return result;
}

function timestamp(value) {
  // Source timestamps are optional.  Keeping them null avoids relying on Date
  // serialization methods missing from the legacy WSH JScript runtime.
  return null;
}

function districtFrom(raw, params) {
  var explicit = "";
  for (var name in params) {
    if (normalized(name).indexOf("район") >= 0 && params[name]) {
      explicit = String(params[name]);
      break;
    }
  }
  var text = normalized(String(raw.title || "") + " " + String(raw.description || ""));
  var aliases = [
    ["Центр", ["центр", "золотой квадрат", "ала-тоо", "эркиндик"]],
    ["Филармония", ["филармони"]], ["ЦУМ", ["цум"]],
    ["ГУМ", ["гум"]], ["Бишкек Парк", ["бишкек парк"]],
    ["Азия Молл", ["азия молл", "asia mall"]],
    ["1000 мелочей", ["1000 мелоч", "тысяча мелоч"]],
    ["Восток-5 мкр", ["восток-5", "восток 5"]],
    ["Аламедин-1", ["аламедин-1", "аламедин 1", "аламидин 1"]],
    ["Кок-Жар", ["кок-жар", "кок жар"]],
    ["Джал", ["джал"]], ["Асанбай", ["асанбай"]],
    ["Орто-Сай", ["орто-сай", "орто сай"]]
  ];
  for (var i = 0; i < aliases.length; i++) {
    for (var j = 0; j < aliases[i][1].length; j++) {
      if (text.indexOf(aliases[i][1][j]) >= 0) return aliases[i][0];
    }
  }
  var match = text.match(/(?:^|\s)(\d{1,2})\s*(?:мкр|микрорайон)/);
  return match ? String(parseInt(match[1], 10)) + " мкр" : (explicit || null);
}

function parseAd(raw, sourceUrl) {
  if (!raw || !raw.id || raw.hide_phone) return null;
  var params = paramsMap(raw.params);
  var roomValue = normalized(params["Количество комнат"] || "");
  var rooms = roomValue === "студия" ? "studio" : (roomValue === "1 комната" ? "1" : "");
  var price = parseInt(raw.price || 0, 10);
  var images = photoUrls(raw);
  var phone = String(raw.mobile || "").replace(/\D/g, "");
  if (phone.length === 9) phone = "+996" + phone;
  else if (phone.length === 10 && phone.charAt(0) === "0") phone = "+996" + phone.substr(1);
  else if (phone.length === 12 && phone.substr(0, 3) === "996") phone = "+" + phone;
  else return null;
  if (parseInt(raw.category_id || 0, 10) !== 2044 || normalized(raw.city) !== "бишкек") return null;
  if (String(raw.currency || "").toUpperCase() !== "KGS" || price < 20000 || price > 40000) return null;
  if (!rooms || images.length < 2) return null;
  var text = normalized(String(raw.title || "") + " " + String(raw.description || ""));
  if (/подсел|койко.?мест|общежит|хостел|комната в квартире|сда[её]тся комната/.test(text)) return null;
  var offerer = normalized(params["Кто предлагает"] || "");
  var realtorService = String(params["Услуги риэлтора"] || "");
  var sellerType = realtorService || /риелтор|риэлтор|агент|агентство/.test(offerer) ? "realtor" : (offerer === "собственник" ? "owner" : "unknown");
  var deposit = parseInt(String(params["Депозит, сом"] || "").replace(/\D/g, ""), 10);
  if (!deposit || deposit === 1) deposit = null;
  return {
    lalafo_id: parseInt(raw.id, 10), source_url: sourceUrl, phone: phone,
    price: price, currency: "KGS", rooms: rooms,
    district: districtFrom(raw, params), city: "Бишкек", deposit: deposit,
    photo_urls: images, category_id: 2044, no_subletting: true,
    owner_listing: sellerType === "owner", seller_type: sellerType,
    source_title: String(raw.title || ""), source_description: String(raw.description || ""),
    source_params: raw.params || [], source_created_at: timestamp(raw.created_time),
    source_updated_at: timestamp(raw.updated_time)
  };
}

function searchUrl(page, offerer) {
  var q = [
    "expand=url", "per-page=20", "category_id=2044", "page=" + page,
    "city_id=103184", "parameters%5B69%5D%5B0%5D=15496",
    "parameters%5B69%5D%5B1%5D=2773", "price%5Bfrom%5D=20000",
    "price%5Bto%5D=40000", "with_feed_banner=true"
  ];
  if (offerer === "owner") q.push("parameters%5B2149%5D%5B0%5D=19057");
  if (offerer === "realtor") q.push("parameters%5B2149%5D%5B0%5D=42340");
  return "https://lalafo.kg/api/search/v3/feed/search?" + q.join("&");
}

function collectCycle() {
  var seen = safeJson(seenPath, {});
  var ids = [];
  var modes = ["owner", "", "realtor"];
  for (var m = 0; m < modes.length; m++) {
    for (var page = 1; page <= 5; page++) {
      try {
        var payload = JSON.parse(request("GET", searchUrl(page, modes[m]), null, null));
        var items = payload.items || [];
        for (var i = 0; i < items.length; i++) {
          var id = parseInt(items[i].id || 0, 10);
          if (id && !seen[id] && ids.indexOf(id) < 0) ids.push(id);
        }
      } catch (error) {
        appendLog("search failed mode=" + modes[m] + " page=" + page + " " + error.message);
      }
    }
  }
  var ads = [];
  for (var n = 0; n < ids.length && ads.length < 120; n++) {
    var id = ids[n];
    try {
      var detailUrl = "https://lalafo.kg/api/search/v3/feed/details/" + id + "?expand=url";
      var raw = JSON.parse(request("GET", detailUrl, null, null));
      var source = raw.url ? (String(raw.url).indexOf("http") === 0 ? String(raw.url) : "https://lalafo.kg" + String(raw.url)) : "https://lalafo.kg/bishkek/ads/id-" + id;
      var ad = parseAd(raw, source);
      seen[id] = new Date().getTime();
      if (ad) ads.push(ad);
    } catch (error) {
      appendLog("detail failed id=" + id + " " + error.message);
    }
  }
  if (ads.length) {
    var response = request("POST", config.relayUrl, JSON.stringify({ads: ads}), {
      "Content-Type": "application/json",
      "X-Lalafo-Relay-Secret": config.relaySecret
    });
    appendLog("relay ok candidates=" + ids.length + " stored=" + ads.length + " response=" + response.substr(0, 160));
  } else {
    appendLog("cycle completed with no new eligible ads; candidates=" + ids.length);
  }
  var cutoff = new Date().getTime() - 14 * 24 * 60 * 60 * 1000;
  for (var key in seen) if (Number(seen[key]) < cutoff) delete seen[key];
  writeText(seenPath, JSON.stringify(seen));
}

var config = safeJson(configPath, null);
if (!config || !config.relayUrl || !config.relaySecret) {
  appendLog("collector-config.json is missing or incomplete");
  WScript.Quit(2);
}
if (!config.userHash) config.userHash = createGuid();
appendLog("collector started");
while (true) {
  try { collectCycle(); } catch (error) { appendLog("cycle failed " + error.message); }
  WScript.Sleep((config.intervalMinutes || 120) * 60 * 1000);
}
