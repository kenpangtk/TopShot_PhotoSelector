import { formatBytes, formatNumber, shortPath } from "../format";
import type { CopyResult, Preflight } from "../types";

interface DialogProps {
  preflight: Preflight;
  profileName: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  onChangeDestination: () => void;
}

export function CopyDialog({ preflight, profileName, busy, onCancel, onConfirm, onChangeDestination }: DialogProps) {
  const missingImages = preflight.missing.filter((item) => item.kind === "image");
  return (
    <div className="modal-backdrop" role="presentation" onClick={busy ? undefined : onCancel}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Confirm copy" onClick={(event) => event.stopPropagation()}>
        <span className="eyebrow">Confirm copy</span>
        <h2>Copy confirmed keeps</h2>
        <p className="modal-copy">
          Topshot copies the files below to the destination root. Existing files are never
          overwritten; identical files are skipped.
        </p>

        <dl className="summary-list">
          <div>
            <dt>Profile</dt>
            <dd>{profileName}</dd>
          </div>
          <div>
            <dt>Source</dt>
            <dd title={preflight.sourceDir}>{shortPath(preflight.sourceDir, 4)}</dd>
          </div>
          <div>
            <dt>Destination</dt>
            <dd title={preflight.destination ?? ""}>{preflight.destination ?? "Not chosen"}</dd>
          </div>
          <div>
            <dt>Files</dt>
            <dd>
              {formatNumber(preflight.files)} files
              <span className="muted">
                {" "}
                · {formatNumber(preflight.images)} images · {formatNumber(preflight.sidecars)} XMP
              </span>
            </dd>
          </div>
          <div>
            <dt>Total size</dt>
            <dd>{formatBytes(preflight.bytes)}</dd>
          </div>
          <div>
            <dt>RAW+JPEG</dt>
            <dd>{preflight.rawJpegMode === "both" ? "Copy both as a pair" : preflight.rawJpegMode === "raw" ? "RAW only" : "JPEG only"}</dd>
          </div>
        </dl>

        {preflight.missing.length > 0 && (
          <div className="warn-box">
            <strong>{preflight.missing.length} source files are missing</strong>
            <span>
              Missing files will be excluded and reported. {missingImages.slice(0, 4).map((item) => item.name).join(", ")}
              {missingImages.length > 4 ? "…" : ""}
            </span>
          </div>
        )}
        {preflight.error && <div className="error-box">{preflight.error}</div>}

        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onChangeDestination} disabled={busy}>
            Change destination
          </button>
          <span className="spacer" />
          <button type="button" className="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="primary" onClick={onConfirm} disabled={busy || !preflight.ok}>
            {busy ? "Starting…" : `Copy ${formatNumber(preflight.files)} files`}
          </button>
        </div>
        <p className="fineprint">
          Rejects and alternates remain in the source folder, untouched. Copies are verified with
          SHA-256 before they are renamed into place.
        </p>
      </div>
    </div>
  );
}

interface SummaryProps {
  result: CopyResult;
  onClose: () => void;
  onOpenDestination: () => void;
}

export function CopySummary({ result, onClose, onOpenDestination }: SummaryProps) {
  const counts = result.counts;
  const title =
    result.status === "canceled" ? "Copy canceled" : result.counts.failed > 0 ? "Copy finished with issues" : "Copy complete";
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal wide" role="dialog" aria-modal="true" aria-label="Copy summary">
        <span className="eyebrow">Verified copy</span>
        <h2>{title}</h2>
        <p className="modal-copy">
          {result.status === "canceled"
            ? "Completed files were kept. Run the copy again to skip identical files and continue."
            : "Every copied file was SHA-256 verified. Originals were not modified."}
        </p>

        <div className="summary-cards">
          <div>
            <span>Copied</span>
            <strong>{formatNumber(counts.copied)}</strong>
            <span className="muted">
              {formatNumber(counts.imagesCopied)} {counts.imagesCopied === 1 ? "image" : "images"} ·{" "}
              {formatNumber(counts.sidecarsCopied)} {counts.sidecarsCopied === 1 ? "sidecar" : "sidecars"}
            </span>
          </div>
          <div>
            <span>Skipped identical</span>
            <strong>{formatNumber(counts.skipped)}</strong>
            <span className="muted">Already present</span>
          </div>
          <div className={counts.failed ? "bad" : ""}>
            <span>Failed</span>
            <strong>{formatNumber(counts.failed)}</strong>
            <span className="muted">Per-file errors</span>
          </div>
          <div className={counts.missing ? "bad" : ""}>
            <span>Missing</span>
            <strong>{formatNumber(counts.missing)}</strong>
            <span className="muted">Excluded</span>
          </div>
        </div>

        <dl className="summary-list">
          <div>
            <dt>Destination</dt>
            <dd>{result.destination}</dd>
          </div>
          <div>
            <dt>Bytes copied</dt>
            <dd>{formatBytes(counts.bytesCopied)}</dd>
          </div>
        </dl>

        {result.failures.length > 0 && (
          <div className="failure-list">
            <strong>Per-file issues</strong>
            <ul>
              {result.failures.slice(0, 20).map((failure, index) => (
                <li key={`${failure.name}-${index}`}>
                  <span>{failure.name}</span>
                  <span className="muted">{failure.error}</span>
                </li>
              ))}
            </ul>
            {result.failures.length > 20 && (
              <span className="muted">{result.failures.length - 20} more in the copy log.</span>
            )}
          </div>
        )}

        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Back to review
          </button>
          <span className="spacer" />
          <button type="button" className="primary" onClick={onOpenDestination}>
            Open Destination
          </button>
        </div>
      </div>
    </div>
  );
}
