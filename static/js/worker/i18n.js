// Worker scan-screen translations (English/Spanish). Scoped to the
// worker PWA only, matching i18n.py's scope on the server side --
// worker_join.html (server-rendered, translated in Python) picks the
// language and stamps it on <body data-lang="..">; this file's T()
// reads that attribute so the scan screen (which runs entirely
// client-side from load) renders in the same language with no extra
// round trip.
(function (root) {
  "use strict";

  var STRINGS = {
    en: {
      bundleLoading: "Loading shift…",
      bundleOfflineReady: "Offline ready ✓",
      bundleStale: "Bundle stale — reconnect",
      bundleCouldNotLoad: "Could not load shift. Check connection and reload.",
      bundleNoneDownloaded: "No shift downloaded yet -- connect once to fetch it.",
      cameraUnavailableSuffix: " (camera unavailable -- use wedge scanner)",
      cameraOffTitle: "Camera off",
      cameraOffHint: "Tap the camera button to scan -- or keep using the wedge scanner, it always works.",
      cameraUnavailableTitle: "Camera unavailable",
      cameraUnavailableHint: "Use the wedge scanner -- it always works.",
      resultOk: "OK",
      resultRejectLabel: "Wrong item -- put it back",
      resultRejectDetail: "Wrong item -- put it back.",
      resultDuplicateLabel: "Already scanned",
      resultDuplicateDetail: "This item is already on the order.",
      dismissButton: "Got it",
      appealButton: "This is right -- appeal",
      logoutButton: "Log out",
      loggingOut: "Logging out…",
      logoutConfirmUnsynced: "{count} scan(s) from this shift haven't synced yet (no connection?). They are saved on this phone and will send next time it's online. Log out anyway?",
      manualEntryButton: "Type barcode",
      manualEntryTitle: "Type barcode",
      manualEntryPlaceholder: "Barcode or SKU",
      manualEntrySubmit: "Submit",
      manualEntryCancel: "Cancel",
      appealModalTitle: "Appeal this reject",
      appealInstructions: "Take a photo of the item so your manager can review it.",
      appealTakePhoto: "Take photo",
      appealRetakePhoto: "Retake photo",
      appealNotePlaceholder: "Add a note (optional)",
      appealSubmit: "Submit appeal",
      appealCancel: "Cancel",
      appealPhotoRequired: "A photo is required to appeal.",
      appealQueued: "Appeal saved -- your manager will review it.",
      summaryTitle: "Shift summary",
      summaryScanned: "scanned",
      summaryCaught: "caught",
      summaryDuplicate: "duplicate",
      summaryNeedsReview: "needs review",
      summaryDone: "Done",
    },
    es: {
      bundleLoading: "Cargando turno…",
      bundleOfflineReady: "Listo sin conexión ✓",
      bundleStale: "Datos desactualizados — reconecta",
      bundleCouldNotLoad: "No se pudo cargar el turno. Revisa tu conexión y recarga.",
      bundleNoneDownloaded: "Aún no se ha descargado el turno -- conéctate una vez para obtenerlo.",
      cameraUnavailableSuffix: " (cámara no disponible -- usa el lector de mano)",
      cameraOffTitle: "Cámara apagada",
      cameraOffHint: "Toca el botón de cámara para escanear -- o usa el lector de mano, siempre funciona.",
      cameraUnavailableTitle: "Cámara no disponible",
      cameraUnavailableHint: "Usa el lector de mano -- siempre funciona.",
      resultOk: "Correcto",
      resultRejectLabel: "Artículo incorrecto -- vuelve a colocarlo",
      resultRejectDetail: "Artículo incorrecto -- vuelve a colocarlo.",
      resultDuplicateLabel: "Ya escaneado",
      resultDuplicateDetail: "Este artículo ya está en el pedido.",
      dismissButton: "Entendido",
      appealButton: "Esto es correcto -- apelar",
      logoutButton: "Cerrar sesión",
      loggingOut: "Cerrando sesión…",
      logoutConfirmUnsynced: "{count} escaneo(s) de este turno aún no se han sincronizado (¿sin conexión?). Están guardados en este teléfono y se enviarán la próxima vez que haya conexión. ¿Cerrar sesión de todas formas?",
      manualEntryButton: "Escribir código",
      manualEntryTitle: "Escribir código de barras",
      manualEntryPlaceholder: "Código de barras o SKU",
      manualEntrySubmit: "Enviar",
      manualEntryCancel: "Cancelar",
      appealModalTitle: "Apelar este rechazo",
      appealInstructions: "Toma una foto del artículo para que tu gerente pueda revisarlo.",
      appealTakePhoto: "Tomar foto",
      appealRetakePhoto: "Tomar otra foto",
      appealNotePlaceholder: "Agregar una nota (opcional)",
      appealSubmit: "Enviar apelación",
      appealCancel: "Cancelar",
      appealPhotoRequired: "Se requiere una foto para apelar.",
      appealQueued: "Apelación guardada -- tu gerente la revisará.",
      summaryTitle: "Resumen del turno",
      summaryScanned: "escaneados",
      summaryCaught: "detectados",
      summaryDuplicate: "duplicados",
      summaryNeedsReview: "necesitan revisión",
      summaryDone: "Listo",
    },
  };

  function currentLang() {
    var lang = document.body && document.body.getAttribute("data-lang");
    return STRINGS[lang] ? lang : "en";
  }

  function T(key, vars) {
    var table = STRINGS[currentLang()];
    var str = (table && table[key]) || STRINGS.en[key] || key;
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        str = str.replace("{" + k + "}", vars[k]);
      });
    }
    return str;
  }

  var I18n = { T: T, STRINGS: STRINGS, currentLang: currentLang };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = I18n;
  }
  if (root) {
    root.AutorackI18n = I18n;
  }
})(typeof window !== "undefined" ? window : null);
