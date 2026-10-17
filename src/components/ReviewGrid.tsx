import { useEffect, useMemo, useRef, useState } from "react";
import type { Burst, Photo } from "../types";
import PhotoTile from "./PhotoTile";

interface Props {
  photos: Photo[];
  bursts: Burst[];
  groupBursts: boolean;
  expanded: Set<number>;
  expandedDuplicates: Set<number>;
  focusedId: number | null;
  resetKey: string;
  onFocus: (id: number) => void;
  onHover: (id: number | null) => void;
  onToggle: (photo: Photo) => void;
  onOpen: (id: number) => void;
  onToggleBurst: (id: number) => void;
  onToggleDuplicate: (id: number) => void;
  onSwapPhoto: (photo: Photo, kind: "duplicate" | "burst", groupId: number) => void;
}

interface SingleItem {
  type: "single";
  photo: Photo;
  key: string;
}
interface BurstItem {
  type: "burst";
  id: number;
  members: Photo[];
  cover: Photo;
  weak: boolean;
  key: string;
}
interface DuplicateItem {
  type: "duplicate";
  id: number;
  members: Photo[];
  cover: Photo;
  key: string;
}
type Item = SingleItem | BurstItem | DuplicateItem;

const PAGE = 240;

export default function ReviewGrid({
  photos,
  bursts,
  groupBursts,
  expanded,
  expandedDuplicates,
  focusedId,
  resetKey,
  onFocus,
  onHover,
  onToggle,
  onOpen,
  onToggleBurst,
  onToggleDuplicate,
  onSwapPhoto,
}: Props) {
  const [limit, setLimit] = useState(PAGE);
  const sentinel = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setLimit(PAGE);
  }, [resetKey]);

  const burstMeta = useMemo(() => {
    const map = new Map<number, Burst>();
    for (const burst of bursts) map.set(burst.id, burst);
    return map;
  }, [bursts]);

  const items = useMemo<Item[]>(() => {
    if (!groupBursts) {
      return photos.map((photo) => ({ type: "single", photo, key: `p-${photo.id}` }));
    }
    const byBurst = new Map<number, Photo[]>();
    const byDuplicate = new Map<number, Photo[]>();
    for (const photo of photos) {
      if (photo.duplicateGroup != null && (photo.duplicateSize ?? 0) > 1) {
        const members = byDuplicate.get(photo.duplicateGroup) ?? [];
        members.push(photo);
        byDuplicate.set(photo.duplicateGroup, members);
      } else if (photo.burstId != null && (photo.burstSize ?? 0) > 1) {
        const members = byBurst.get(photo.burstId) ?? [];
        members.push(photo);
        byBurst.set(photo.burstId, members);
      }
    }
    const seenBursts = new Set<number>();
    const seenDuplicates = new Set<number>();
    const result: Item[] = [];
    for (const photo of photos) {
      if (photo.duplicateGroup != null && byDuplicate.has(photo.duplicateGroup)) {
        if (seenDuplicates.has(photo.duplicateGroup)) continue;
        seenDuplicates.add(photo.duplicateGroup);
        const members = byDuplicate.get(photo.duplicateGroup)!;
        const cover = members.reduce((best, member) =>
          (member.rankScore ?? 0) > (best.rankScore ?? 0) ? member : best,
        );
        result.push({
          type: "duplicate",
          id: photo.duplicateGroup,
          members,
          cover,
          key: `d-${photo.duplicateGroup}`,
        });
      } else if (photo.burstId != null && byBurst.has(photo.burstId)) {
        if (seenBursts.has(photo.burstId)) continue;
        seenBursts.add(photo.burstId);
        const members = byBurst.get(photo.burstId)!;
        const cover = members.find((member) => member.burstRank === 1) ?? members[0];
        result.push({
          type: "burst",
          id: photo.burstId,
          members,
          cover,
          weak: burstMeta.get(photo.burstId)?.weak ?? false,
          key: `b-${photo.burstId}`,
        });
      } else {
        result.push({ type: "single", photo, key: `p-${photo.id}` });
      }
    }
    return result;
  }, [photos, groupBursts, burstMeta]);

  useEffect(() => {
    const node = sentinel.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setLimit((value) => (value < items.length ? value + PAGE : value));
        }
      },
      { rootMargin: "600px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [items.length, limit]);

  const shown = items.slice(0, limit);
  const hasMore = limit < items.length;

  if (photos.length === 0) {
    return (
      <div className="grid-empty">
        <p>No photos match this filter.</p>
        <p className="muted">Switch filters to see the rest of the shoot.</p>
      </div>
    );
  }

  return (
    <>
      <div className="photo-grid">
        {shown.map((item) => {
          if (item.type === "single") {
            return (
              <PhotoTile
                key={item.key}
                photo={item.photo}
                focused={focusedId === item.photo.id}
                onFocus={onFocus}
                onHover={onHover}
                onToggle={onToggle}
                onOpen={onOpen}
              />
            );
          }
          if (item.type === "duplicate") {
            const isExpanded = expandedDuplicates.has(item.id);
            if (!isExpanded) {
              return (
                <div className="burst-collapsed duplicate-collapsed" key={item.key}>
                  <PhotoTile
                    photo={item.cover}
                    focused={focusedId === item.cover.id}
                    onFocus={onFocus}
                    onHover={onHover}
                    onToggle={onToggle}
                    onOpen={onOpen}
                    hideBurstBadge
                  />
                  <button
                    type="button"
                    className="burst-expand duplicate-expand"
                    onClick={(event) => {
                      event.stopPropagation();
                      onToggleDuplicate(item.id);
                    }}
                    aria-label={`Expand ${item.members.length} similar photos`}
                  >
                    <span className="burst-count">{item.members.length}</span>
                    <span>similar · expand</span>
                  </button>
                  <span className="duplicate-best">Best pick</span>
                </div>
              );
            }
            return (
              <section className="burst-panel duplicate-panel" key={item.key} aria-label={`Similar group ${item.id}`}>
                <header className="burst-head">
                  <div>
                    <strong>Similar group {item.id}</strong>
                    <span className="muted">
                      {item.members.length} frames · best pick uses eyes, smiles, and visual quality
                    </span>
                  </div>
                  <button type="button" className="ghost small" onClick={() => onToggleDuplicate(item.id)}>
                    Collapse
                  </button>
                </header>
                <div className="burst-members">
                  {item.members.map((member) => {
                    const isBest = member.id === item.cover.id;
                    return (
                      <div className="group-member" key={member.id}>
                        <PhotoTile
                          photo={member}
                          focused={focusedId === member.id}
                          onFocus={onFocus}
                          onHover={onHover}
                          onToggle={onToggle}
                          onOpen={onOpen}
                          hideBurstBadge
                          bestOfGroup={isBest}
                          userPick={member.selected && !isBest}
                        />
                        <button
                          type="button"
                          className={`swap-button ${member.selected ? "current" : ""}`}
                          disabled={member.selected}
                          onClick={() => onSwapPhoto(member, "duplicate", item.id)}
                        >
                          {member.selected ? "Current pick" : "Use this instead"}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          }
          const isExpanded = expanded.has(item.id);
          if (!isExpanded) {
            return (
              <div className="burst-collapsed" key={item.key}>
                <PhotoTile
                  photo={item.cover}
                  focused={focusedId === item.cover.id}
                  onFocus={onFocus}
                  onHover={onHover}
                  onToggle={onToggle}
                  onOpen={onOpen}
                  hideBurstBadge
                />
                <button
                  type="button"
                  className="burst-expand"
                  onClick={(event) => {
                    event.stopPropagation();
                    onToggleBurst(item.id);
                  }}
                  aria-label={`Expand ${item.members.length} frame burst`}
                >
                  <span className="burst-count">{item.members.length}</span>
                  <span>frames · expand</span>
                </button>
                {item.weak && <span className="burst-weak">Weak burst</span>}
              </div>
            );
          }
          return (
            <section className="burst-panel" key={item.key} aria-label={`Burst ${item.id}`}>
              <header className="burst-head">
                <div>
                  <strong>Burst {item.id}</strong>
                  <span className="muted">
                    {item.members.length} frames · best pick rank 1
                  </span>
                  {item.weak && <span className="badge badge-review">Weak burst — review</span>}
                </div>
                <button type="button" className="ghost small" onClick={() => onToggleBurst(item.id)}>
                  Collapse
                </button>
              </header>
              <div className="burst-members">
                {item.members.map((member) => {
                  const isBest = member.burstRank === 1;
                  return (
                    <div className="group-member" key={member.id}>
                      <PhotoTile
                        photo={member}
                        focused={focusedId === member.id}
                        onFocus={onFocus}
                        onHover={onHover}
                        onToggle={onToggle}
                        onOpen={onOpen}
                        hideBurstBadge
                        bestOfGroup={isBest}
                        userPick={member.selected && !isBest}
                      />
                      <button
                        type="button"
                        className={`swap-button ${member.selected ? "current" : ""}`}
                        disabled={member.selected}
                        onClick={() => onSwapPhoto(member, "burst", item.id)}
                      >
                        {member.selected ? "Current pick" : "Use this instead"}
                      </button>
                    </div>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>
      {hasMore && (
        <div className="grid-more" ref={sentinel}>
          Showing {Math.min(limit, items.length)} of {items.length} · scroll for more
        </div>
      )}
    </>
  );
}
