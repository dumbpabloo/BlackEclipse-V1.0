import { describe, expect, it } from "vitest";

import {
  coerceVideoBlob,
  coerceVideoJobRef,
  coerceVideoJobStatus,
  videoJobLabel,
} from "./video-view";

describe("video-view coercers", () => {
  it("keeps only well-formed job references", () => {
    const ref = coerceVideoJobRef({
      jobId: "job_1",
      projectId: "vprj_a",
      projectName: "Recap",
      kind: "render",
      mode: "final",
    });
    expect(ref).toEqual({
      jobId: "job_1",
      projectId: "vprj_a",
      projectName: "Recap",
      kind: "render",
      mode: "final",
      ofJobId: null,
    });
    expect(coerceVideoJobRef({ jobId: "job_1" })).toBeNull();
    expect(coerceVideoJobRef({ jobId: "j", projectId: "p", kind: "nope" })).toBeNull();
    expect(coerceVideoJobRef(null)).toBeNull();
  });

  it("dedupes the blob and caps at 8", () => {
    const jobs = Array.from({ length: 12 }, (_, i) => ({
      jobId: `job_${i}`,
      projectId: "vprj_a",
      kind: "render",
      mode: "draft",
    }));
    jobs.push({ jobId: "job_0", projectId: "vprj_a", kind: "render", mode: "draft" });
    const refs = coerceVideoBlob({ type: "video", jobs });
    expect(refs).toHaveLength(8);
    expect(new Set(refs.map((r) => r.jobId)).size).toBe(8);
  });

  it("returns [] for an absent or odd blob", () => {
    expect(coerceVideoBlob(undefined)).toEqual([]);
    expect(coerceVideoBlob("nope")).toEqual([]);
    expect(coerceVideoBlob({ jobs: "nope" })).toEqual([]);
  });

  it("reports a completed final render with its artifact", () => {
    const status = coerceVideoJobStatus("job_1", {
      jobId: "job_1",
      status: "SUCCEEDED",
      progress: 1,
      terminal: true,
      output: {
        status: "completed",
        action: "render",
        mode: "final",
        durationSeconds: 12.5,
        artifact: { id: "art_1", name: "Recap.mp4" },
      },
    });
    expect(status.terminal).toBe(true);
    expect(status.failed).toBe(false);
    expect(status.artifactId).toBe("art_1");
    expect(status.durationSeconds).toBe(12.5);
    expect(status.hasThumbnail).toBe(false);
  });

  it("reports a real poster only when the runtime recorded one", () => {
    const status = coerceVideoJobStatus("job_t", {
      status: "SUCCEEDED",
      terminal: true,
      output: {
        artifact: { id: "art_2", name: "Recap.mp4", thumbnailKey: "thumbs/art_2.png" },
      },
    });
    expect(status.hasThumbnail).toBe(true);
    const noKey = coerceVideoJobStatus("job_u", {
      status: "SUCCEEDED",
      output: { artifact: { id: "art_3", name: "Recap.mp4" } },
    });
    expect(noKey.hasThumbnail).toBe(false);
  });

  it("treats a failed job as terminal without inventing output", () => {
    const status = coerceVideoJobStatus("job_2", {
      status: "FAILED",
      terminal: true,
      errorClass: "INSPECTION_FAILED",
      error: "the rendered video failed inspection: text is clipped",
      output: null,
    });
    expect(status.terminal).toBe(true);
    expect(status.failed).toBe(true);
    expect(status.errorClass).toBe("INSPECTION_FAILED");
    expect(status.artifactId).toBeNull();
  });

  it("clamps progress and falls back for junk", () => {
    const status = coerceVideoJobStatus("job_3", { status: "RUNNING", progress: 7 });
    expect(status.progress).toBe(1);
    const empty = coerceVideoJobStatus("job_4", null);
    expect(empty.status).toBe("UNKNOWN");
    expect(empty.terminal).toBe(false);
  });

  it("counts preview images and inspection findings", () => {
    const preview = coerceVideoJobStatus("job_5", {
      status: "SUCCEEDED",
      terminal: true,
      output: { action: "preview", previews: [{ kind: "strip" }, { kind: "frame" }] },
    });
    expect(preview.previewCount).toBe(2);
    const inspect = coerceVideoJobStatus("job_6", {
      status: "SUCCEEDED",
      terminal: true,
      output: { action: "inspect", findingsTotal: 3, findingsErrors: 1 },
    });
    expect(inspect.findingsTotal).toBe(3);
    expect(inspect.findingsErrors).toBe(1);
  });

  it("labels each kind honestly", () => {
    expect(videoJobLabel({ jobId: "j", projectId: "p", projectName: null, kind: "render", mode: "final", ofJobId: null })).toBe("Final render");
    expect(videoJobLabel({ jobId: "j", projectId: "p", projectName: null, kind: "render", mode: "draft", ofJobId: null })).toBe("Draft render");
    expect(videoJobLabel({ jobId: "j", projectId: "p", projectName: null, kind: "preview", mode: null, ofJobId: null })).toBe("Preview");
    expect(videoJobLabel({ jobId: "j", projectId: "p", projectName: null, kind: "repair", mode: "draft", ofJobId: "j0" })).toBe("Repair (draft)");
    expect(videoJobLabel({ jobId: "j", projectId: "p", projectName: null, kind: "inspect", mode: null, ofJobId: null })).toBe("Frame inspection");
  });
});
