// Envio do PDF em partes. O proxy do Next.js limita cada requisição a 10 MiB
// (proxyClientMaxBodySize), então o arquivo é dividido em blocos de 8 MiB.
import { api, ApiError, type Project } from "./api";

export const CHUNK_SIZE = 8 * 1024 * 1024;

export interface UploadOptions { name?: string; password?: string; ocr_languages?: string }

export async function startUpload(file: File): Promise<string> {
  const r = await api<{ upload_id: string }>("/api/uploads", {
    method: "POST", json: { filename: file.name, size: file.size, chunk_size: CHUNK_SIZE },
  });
  return r.upload_id;
}

export async function sendChunks(uploadId: string, file: File, onProgress?: (frac: number) => void): Promise<void> {
  const status = await api<{ chunks: number; received: number[] }>(`/api/uploads/${uploadId}`);
  const done = new Set(status.received); // retomada: pula partes já recebidas
  for (let i = 0; i < status.chunks; i++) {
    if (!done.has(i)) {
      const blob = file.slice(i * CHUNK_SIZE, Math.min(file.size, (i + 1) * CHUNK_SIZE));
      for (let attempt = 0; ; attempt++) {
        try {
          await api(`/api/uploads/${uploadId}/chunks/${i}`, {
            method: "PUT", body: blob, headers: { "Content-Type": "application/octet-stream" },
          });
          break;
        } catch (e) {
          const transient = !(e instanceof ApiError) || e.status >= 500;
          if (!transient || attempt >= 3) throw e;
          await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
        }
      }
    }
    onProgress?.((i + 1) / status.chunks);
  }
}

export async function completeUpload(uploadId: string, opts: UploadOptions) {
  return api<{ project: Project }>(`/api/uploads/${uploadId}/complete`, { method: "POST", json: opts });
}
