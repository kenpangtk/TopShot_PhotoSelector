import { useState } from "react";
import { assetUrl } from "../api";
import { formatBytes } from "../format";
import type { Photo } from "../types";

interface Props {
  photo: Photo;
  focused: boolean;
  onFocus: (id: number) => void;
  onHover?: (id: number | null) => void;
  onToggle: (photo: Photo) => void;
  onOpen: (id: number) => void;
  hideBurstBadge?: boolean;
  bestOfGroup?: boolean;
  userPick?: boolean;
}

function SuggestionBadge({ photo }: { photo: Photo }) {
  if (photo.status === "missing") {
    return <span className="badge badge-missing">Missing</span>;
  }
  if (photo.status === "failed") {
    return <span className="badge badge-missing">Failed</span>;
  }
  if (photo.decision === "keep") {
    return <span className="badge badge-keep">Keep</span>;
  }
  if (photo.decision === "reject") {
    return <span className="badge badge-reject">Reject</span>;
  }
  if (photo.decision === "alternate" || photo.suggestion === "alternate") {
    return <span className="badge badge-alternate">Alt</span>;
  }
  if (photo.effectiveState === "reject") {
    return <span className="badge badge-reject">Reject</span>;
  }
  if (photo.effectiveState === "keep") {
    return <span className="badge badge-keep">Keep</span>;
  }
  return <span className="badge badge-review">Review</span>;
}

export default function PhotoTile({
  photo,
  focused,
  onFocus,
  onHover,
  onToggle,
  onOpen,
  hideBurstBadge,
  bestOfGroup,
  userPick,
}: Props) {
  const [loaded, setLoaded] = useState(false);
  const [broken, setBroken] = useState(false);
  const confidence = photo.confidence != null ? `${Math.round(photo.confidence * 100)}%` : null;
  const isPair = photo.pairRole === "jpeg" && photo.pairedFileId != null;
  const title = [
    photo.name,
    photo.reason,
    `${photo.width ?? "?"}×${photo.height ?? "?"}`,
    formatBytes(photo.size),
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <article
      className={`tile ${focused ? "focused" : ""} ${photo.selected ? "selected" : ""} ${
        photo.status === "missing" || photo.status === "failed" ? "unavailable" : ""
      }`}
      onClick={() => onFocus(photo.id)}
      onMouseEnter={() => onHover?.(photo.id)}
      onMouseLeave={() => onHover?.(null)}
      onDoubleClick={() => onOpen(photo.id)}
      title={title}
      data-photo-id={photo.id}
    >
      <div className="tile-media">
        {!loaded && !broken && <div className="tile-placeholder" />}
        {broken ? (
          <div className="tile-broken">Preview unavailable</div>
        ) : (
          <img
            src={assetUrl(photo.thumbUrl)}
            alt={photo.name}
            loading="lazy"
            decoding="async"
            draggable={false}
            onLoad={() => setLoaded(true)}
            onError={() => setBroken(true)}
          />
        )}
        <div className="tile-shade" />
        <div className="tile-top">
          <button
            type="button"
            className={`check ${photo.selected ? "checked" : ""}`}
            aria-label={photo.selected ? `Deselect ${photo.name}` : `Select ${photo.name}`}
            aria-pressed={photo.selected}
            disabled={photo.status === "missing"}
            onClick={(event) => {
              event.stopPropagation();
              onToggle(photo);
            }}
          >
            {photo.selected ? "✓" : ""}
          </button>
          <div className="badges">
            <SuggestionBadge photo={photo} />
            {bestOfGroup && <span className="badge badge-best">Best</span>}
            {userPick && <span className="badge badge-userpick">Your pick</span>}
            {!hideBurstBadge && photo.burstSize && photo.burstSize > 1 && (
              <span className="badge badge-burst">{photo.burstSize} frames</span>
            )}
            {(photo.documentationScore ?? 0) >= 0.45 && (
              <span className="badge badge-doc" title={photo.documentationDetail ?? "Documentation/text photo"}>
                Doc
              </span>
            )}
            {photo.peopleCount > 0 && (
              <span className="badge badge-people">
                {photo.peopleCount} {photo.peopleCount === 1 ? "person" : "people"}
              </span>
            )}
            {photo.smileCount > 0 && <span className="badge badge-smile">Smiling</span>}
            {photo.peopleCount > 0 && (photo.eyesOpenScore ?? 0) >= 0.75 && (
              <span className="badge badge-eyes">Eyes open</span>
            )}
            {isPair && <span className="badge badge-pair">RAW+JPEG</span>}
            {photo.sidecar && <span className="badge badge-xmp">XMP</span>}
          </div>
        </div>
        <div className="tile-bottom">
          <span className="tile-name">{photo.name}</span>
          <span className="tile-meta">
            {photo.rating ? <span className="rating-mini">{"★".repeat(photo.rating)}</span> : null}
            {confidence && <span>{confidence}</span>}
          </span>
        </div>
        {photo.reason && (
          <div className="tile-reason">
            <span>{photo.reason}</span>
          </div>
        )}
      </div>
    </article>
  );
}
