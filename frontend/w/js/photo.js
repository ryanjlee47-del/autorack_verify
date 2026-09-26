// Problem photos: take one with the phone's camera app, shrink it, keep it.
//
// Uses a file input with `capture` rather than getUserMedia: it opens the
// phone's own camera (focus, flash, a proper shutter), needs no extra
// permission prompt, and works the same on iOS and Android. The picture is
// downscaled to a ~1280px JPEG (~150 KB) before it's stored, so a queue of
// photos taken offline doesn't fill the phone or take ages on bad Wi-Fi.

const MAX_SIDE = 1280;
const QUALITY = 0.72;

/** Open the camera; resolves with a compressed JPEG Blob, or null if cancelled. */
export function takePhoto() {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.setAttribute("capture", "environment");
    input.hidden = true;
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(value);
    };
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      if (!file) return done(null);
      try {
        done(await compress(file));
      } catch {
        done(null);
      }
    });
    // No change event when the camera is dismissed; "cancel" exists in newer browsers.
    input.addEventListener("cancel", () => done(null));
    document.body.appendChild(input);
    input.click();
  });
}

async function decode(file) {
  if (window.createImageBitmap) {
    try {
      return await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      /* fall through to <img> */
    }
  }
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export async function compress(file) {
  const img = await decode(file);
  const w = img.width || img.naturalWidth;
  const hgt = img.height || img.naturalHeight;
  const scale = Math.min(1, MAX_SIDE / Math.max(w, hgt));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(hgt * scale);
  canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
  if (img.close) img.close();
  const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", QUALITY));
  if (!blob) throw new Error("encode failed");
  return blob;
}
