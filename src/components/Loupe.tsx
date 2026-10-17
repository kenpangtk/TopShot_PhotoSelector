import { useCallback, useEffect, useRef, useState } from "react";
import { assetUrl } from "../api";
import { formatBytes } from "../format";
import type { Photo } from "../types";

interface Props {
  photo: Photo;
  index: number;
  total: number;
  similarGroupsRemaining: number;
  similarGroupsTotal: number;
  onClose: () => void;
  onPrev: () => void;
  onNext: () => void;
  onToggleSelect: (photo: Photo) => void;
  onReject: (photo: Photo) => void;
  onRate: (photo: Photo, rating: number) => void;
  onUnrate: (photo: Photo) => void;
  groupMembers: Photo[];
  groupKind: "duplicate" | "burst" | null;
  groupId: number | null;
  onJumpToPhoto: (id: number) => void;
  onSwapGroup: (photo: Photo, kind: "duplicate" | "burst", groupId: number) => void;
  onUndo: () => void;
  canUndo: boolean;
  zoom: "fit" | "fit15" | "100" | "200";
  onToggleZoom: () => void;
  showStrip: boolean;
  onToggleStrip: () => void;
}

export default function Loupe({
  photo,
  index,
  total,
  similarGroupsRemaining,
  similarGroupsTotal,
  onClose,
  onPrev,
  onNext,
  onToggleSelect,
  onReject,
  onRate,
  onUnrate,
  groupMembers,
  groupKind,
  groupId,
  onJumpToPhoto,
  onSwapGroup,
  onUndo,
  canUndo,
  zoom,
  onToggleZoom,
  showStrip,
  onToggleStrip,
}: Props) {
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [natural, setNatural] = useState<{ width: number; height: number } | null>(null);
  const [fitSize, setFitSize] = useState<{ width: number; height: number } | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const dragMoved = useRef(false);
  const hoverTimer = useRef<number | null>(null);
  const preloaded = useRef(new Set<string>());

  useEffect(() => {
    setLoaded(false);
    setLoadError(false);
    setNatural(null);
    setFitSize(null);
    let cancelled = false;
    let checks = 0;
    const checkImage = () => {
      if (cancelled) return;
      const image = imgRef.current;
      if (image?.complete) {
        if (image.naturalWidth > 0) {
          setLoaded(true);
          setNatural({ width: image.naturalWidth, height: image.naturalHeight });
        } else {
          setLoadError(true);
        }
        return;
      }
      checks += 1;
      if (checks > 100) {
        setLoadError(true);
        return;
      }
      window.setTimeout(checkImage, 100);
    };
    checkImage();
    return () => {
      cancelled = true;
    };
  }, [photo.id]);

  useEffect(() => {
    return () => {
      if (hoverTimer.current != null) window.clearTimeout(hoverTimer.current);
    };
  }, []);

  useEffect(() => {
    for (const member of groupMembers) {
      const url = assetUrl(member.previewUrl);
      if (preloaded.current.has(url)) continue;
      preloaded.current.add(url);
      const image = new Image();
      image.src = url;
    }
  }, [groupMembers]);

  const measureFit = useCallback(() => {
    const image = imgRef.current;
    if (!image) return;
    const rect = image.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      setFitSize({ width: rect.width, height: rect.height });
    }
  }, []);

  useEffect(() => {
    if (zoom !== "fit") return;
    const frame = window.requestAnimationFrame(measureFit);
    const node = canvasRef.current;
    const observer = node ? new ResizeObserver(measureFit) : null;
    if (node) observer?.observe(node);
    return () => {
      window.cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, [zoom, photo.id, natural, measureFit]);

  const confidence = photo.confidence != null ? Math.round(photo.confidence * 100) : null;
  const groupBestId =
    groupMembers.length > 0
      ? groupKind === "burst"
        ? (groupMembers.find((member) => member.burstRank === 1) ?? groupMembers[0]).id
        : groupMembers.reduce((best, member) =>
            (member.rankScore ?? 0) > (best.rankScore ?? 0) ? member : best,
          ).id
      : null;
  const inGroup = groupKind != null && groupId != null && groupMembers.length > 1;
  const fitScale = fitSize && natural ? fitSize.width / natural.width : null;
  const gentleScale = fitScale != null ? Math.min(1, fitScale * 1.5) : 0.5;
  const zoomScale = zoom === "fit15" ? gentleScale : zoom === "200" ? 2 : 1;
  const nextZoom =
    zoom === "fit" ? "1.5×" : zoom === "fit15" ? "100%" : zoom === "100" ? "200%" : "Fit";
  const zoomClass =
    zoom === "fit"
      ? "zoom-fit"
      : zoom === "fit15"
        ? "zoom-fit15"
        : zoom === "100"
          ? "zoom-100"
          : "zoom-200";
  const useGroupPick = () => {
    if (!inGroup) return;
    onSwapGroup(photo, groupKind, groupId);
    onJumpToPhoto(photo.id);
  };

  return (
    <div className="loupe" role="dialog" aria-modal="true" aria-label={`Loupe: ${photo.name}`}>
      <header className="loupe-head">
        <div className="loupe-head-main">
        <div className="loupe-title">
          <strong>{photo.name}</strong>
          <span className="muted">{photo.relPath}</span>
        </div>
        <div className={`loupe-suggestion s-${photo.effectiveState}`}>
          <span className={`badge badge-${photo.effectiveState}`}>
            {photo.suggestion === "alternate" ? "Alternate" : photo.effectiveState}
          </span>
          {confidence != null && (
            <span className="confidence" title="Suggestion confidence">
              <span className="confidence-bar">
                <span style={{ width: `${confidence}%` }} />
              </span>
              {confidence}%
            </span>
          )}
          <span className="loupe-reason">{photo.reason}</span>
        </div>
        <div className="loupe-actions">
          {inGroup ? (
            <button
              type="button"
              className={`toggle ${photo.selected ? "on" : ""}`}
              aria-pressed={photo.selected}
              disabled={photo.selected}
              onClick={useGroupPick}
              title="Replace the current pick for this group (Enter)"
            >
              {photo.selected ? "✓ Current pick" : "Use this instead"}
            </button>
          ) : (
            <button
              type="button"
              className={`toggle ${photo.selected ? "on" : ""}`}
              aria-pressed={photo.selected}
              onClick={() => onToggleSelect(photo)}
            >
              {photo.selected ? "✓ Selected" : "Select"}
            </button>
          )}
          <button type="button" className="danger-ghost" onClick={() => onReject(photo)}>
            ✕ Exclude
          </button>
          <div className="rating" aria-label="Rating">
            {[1, 2, 3, 4, 5].map((value) => (
              <button
                type="button"
                key={value}
                className={`star ${photo.rating != null && photo.rating >= value ? "on" : ""}`}
                onClick={() => onRate(photo, value)}
                aria-label={`Rate ${value}`}
              >
                ★
              </button>
            ))}
            <button type="button" className="ghost small" onClick={() => onUnrate(photo)}>
              U
            </button>
          </div>
          {inGroup && (
            <button type="button" className="ghost small" onClick={onToggleStrip}>
              {showStrip ? "Hide frames" : "Show frames"}
            </button>
          )}
          <button type="button" className="ghost small" onClick={onToggleZoom} title={`Click to zoom: ${nextZoom}`}>
            {nextZoom}
          </button>
          <button type="button" className="ghost small" onClick={onUndo} disabled={!canUndo} title="Undo the last edit or swap">
            Undo
          </button>
          <button type="button" className="ghost small" onClick={onClose}>
            Esc
          </button>
        </div>
        </div>
        <div className="loupe-shortcuts" aria-label="Loupe keyboard shortcuts">
          <span className="shortcut-heading">Keys</span>
          <span><kbd>1</kbd>–<kbd>5</kbd> rate</span>
          <span><kbd>S</kbd> select</span>
          <span><kbd>X</kbd> exclude</span>
          <span><kbd>U</kbd> unrate</span>
          <span><kbd>←</kbd><kbd>→</kbd> groups</span>
          {inGroup && <span><kbd>↑</kbd><kbd>↓</kbd> alternatives</span>}
          <span><kbd>Click</kbd>/<kbd>Space</kbd> zoom</span>
          <span><kbd>B</kbd> frames</span>
          {inGroup && !photo.selected && (
            <span className="loupe-use-hint"><kbd>⏎</kbd> use instead</span>
          )}
          <span><kbd>⌘Z</kbd> undo</span>
          <span><kbd>Esc</kbd> close</span>
        </div>
      </header>

      <div
        ref={canvasRef}
        className={`loupe-canvas ${zoomClass} ${zoom === "200" ? "zoom-out-cursor" : "zoom-in-cursor"}`}
        onClick={() => {
          if (dragMoved.current) {
            dragMoved.current = false;
            return;
          }
          onToggleZoom();
        }}
        onPointerDown={(event) => {
          if (zoom === "fit") return;
          const node = canvasRef.current;
          if (!node) return;
          dragMoved.current = false;
          drag.current = { x: event.clientX, y: event.clientY, left: node.scrollLeft, top: node.scrollTop };
          node.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (!drag.current || !canvasRef.current) return;
          const dx = event.clientX - drag.current.x;
          const dy = event.clientY - drag.current.y;
          if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragMoved.current = true;
          canvasRef.current.scrollLeft = drag.current.left - dx;
          canvasRef.current.scrollTop = drag.current.top - dy;
        }}
        onPointerUp={() => {
          drag.current = null;
        }}
      >
        {!loaded && (
          <div className="loupe-loading">{loadError ? "Preview unavailable" : "Loading preview…"}</div>
        )}
        <img
          ref={imgRef}
          src={assetUrl(photo.previewUrl)}
          alt={photo.name}
          draggable={false}
          onLoad={(event) => {
            setLoadError(false);
            setLoaded(true);
            setNatural({
              width: event.currentTarget.naturalWidth,
              height: event.currentTarget.naturalHeight,
            });
          }}
          onError={() => {
            setLoaded(false);
            setLoadError(true);
          }}
          style={
            zoom !== "fit" && natural
              ? { width: natural.width * zoomScale, maxWidth: "none", maxHeight: "none" }
              : undefined
          }
          className={loaded ? "loaded" : ""}
        />
      </div>

      <footer className="loupe-foot">
        <button type="button" className="ghost" onClick={onPrev} disabled={total <= 1}>
          ← Previous
        </button>
        <div className="loupe-meta">
          {similarGroupsTotal > 0 && (
            <span
              className="loupe-group-progress"
              title="Similar/duplicate groups from the filter you opened that you have not viewed yet in the loupe"
              aria-live="polite"
            >
              Similar groups <strong>{similarGroupsRemaining}</strong> / {similarGroupsTotal} left
            </span>
          )}
          <span>
            {index + 1} / {total}
          </span>
          <span>{photo.width && photo.height ? `${photo.width}×${photo.height}` : "—"}</span>
          <span>{formatBytes(photo.size)}</span>
          <span>{photo.kind.toUpperCase()}</span>
          {photo.camera && <span>{photo.camera}</span>}
          {photo.peopleCount > 0 && (
            <span>
              {photo.peopleCount} {photo.peopleCount === 1 ? "person" : "people"}
            </span>
          )}
          {photo.eyesExpected > 0 && (
            <span title="Open-eye detections from Haar cascades; glasses and angles can miss">
              Eyes found {photo.eyeCount}/{photo.eyesExpected}
            </span>
          )}
          {photo.smileCount > 0 && (
            <span title="Smiling faces detected">
              Smiling {photo.smileCount}/{photo.peopleCount}
            </span>
          )}
          {photo.peopleQuality != null && photo.peopleCount > 0 && (
            <span title="Face quality from open eyes and smiles">
              Face quality {Math.round(photo.peopleQuality * 100)}
            </span>
          )}
          {photo.interestingScore != null && (
            <span title="Heuristic visual interest score">Interest {Math.round(photo.interestingScore * 100)}</span>
          )}
          {photo.rankScore != null && <span title="Composite rank score">Rank {Math.round(photo.rankScore * 100)}</span>}
          {photo.burstId != null && <span>Burst {photo.burstId}</span>}
          {photo.duplicateGroup != null && <span>Duplicate group {photo.duplicateGroup}</span>}
          <span>
            {zoom === "fit"
              ? "Fit"
              : zoom === "fit15"
                ? "1.5× fit"
                : zoom === "100"
                  ? "100% proxy"
                  : "200% proxy"}
          </span>
        </div>
        <button type="button" className="ghost" onClick={onNext} disabled={total <= 1}>
          Next →
        </button>
      </footer>

      {inGroup && showStrip && (
        <div className="loupe-strip" aria-label={`${groupKind === "duplicate" ? "Similar" : "Burst"} alternatives`}>
          <div className="loupe-strip-head">
            <strong>{groupKind === "duplicate" ? "Similar frames" : "Burst frames"}</strong>
            <span className="muted">
              {groupMembers.length} frames · click a frame to view it, or use it instead · B hides strip
            </span>
          </div>
          <div className="loupe-strip-track">
            {groupMembers.map((member) => {
              const isCurrent = member.id === photo.id;
              const isBest = member.id === groupBestId;
              return (
                <div
                  key={member.id}
                  className={`strip-item ${isCurrent ? "current" : ""} ${member.selected ? "picked" : ""}`}
                  onMouseEnter={() => {
                    if (isCurrent) return;
                    if (hoverTimer.current != null) window.clearTimeout(hoverTimer.current);
                    hoverTimer.current = window.setTimeout(() => {
                      hoverTimer.current = null;
                      onJumpToPhoto(member.id);
                    }, 140);
                  }}
                  onMouseLeave={() => {
                    if (hoverTimer.current != null) {
                      window.clearTimeout(hoverTimer.current);
                      hoverTimer.current = null;
                    }
                  }}
                >
                  <button
                    type="button"
                    className="strip-thumb"
                    onClick={() => onJumpToPhoto(member.id)}
                    title={`${member.name}${isBest ? " · AI best" : ""}`}
                  >
                    <img src={assetUrl(member.thumbUrl)} alt={member.name} loading="lazy" />
                    <span className="strip-name">{member.name}</span>
                    {member.selected && <span className="strip-check">✓</span>}
                    {isBest && <span className="strip-best">Best</span>}
                  </button>
                  <button
                    type="button"
                    className={`strip-action ${member.selected ? "current" : ""}`}
                    disabled={member.selected}
                    onClick={() => {
                      onSwapGroup(member, groupKind, groupId);
                      onJumpToPhoto(member.id);
                    }}
                  >
                    {member.selected ? "Current pick" : "Use instead"}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
