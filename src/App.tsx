import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  initEngine,
  isTauri,
  listenForFolderDrop,
  openPath,
  pickFolder,
  rpc,
} from "./api";
import { CopyDialog, CopySummary as CopySummaryModal } from "./components/CopyDialog";
import Loupe from "./components/Loupe";
import ProgressView from "./components/ProgressView";
import ReviewGrid from "./components/ReviewGrid";
import Welcome from "./components/Welcome";
import { basename, formatBytes, formatNumber, shortPath } from "./format";
import type {
  AppInfo,
  CopyResult,
  Counts,
  Decision,
  FilterKey,
  Job,
  Photo,
  PhotosResponse,
  Preflight,
  PreferencesSummary,
  Project,
  ScanStats,
  User,
} from "./types";

type UndoEntry =
  | {
      kind: "photo";
      photoId: number;
      before: { selected: boolean; decision: Decision; rating: number | null };
      label: string;
    }
  | {
      kind: "group";
      groupId: number;
      groupType: "duplicate" | "burst";
      previousId: number | null;
      label: string;
    };

interface PhotoUpdateResponse {
  photo: Photo;
  counts: Counts;
}

const readError = (error: unknown) => (error instanceof Error ? error.message : String(error));

function stepState(index: number, active: number, doneBefore: boolean) {
  if (index < active) return "done";
  if (index === active) return "active";
  return doneBefore ? "done" : "";
}

function buildGroupSequence(photos: Photo[], currentId: number | null): Photo[] {
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
  const result: Photo[] = [];
  const seenBursts = new Set<number>();
  const seenDuplicates = new Set<number>();
  for (const photo of photos) {
    if (photo.duplicateGroup != null && byDuplicate.has(photo.duplicateGroup)) {
      if (seenDuplicates.has(photo.duplicateGroup)) continue;
      seenDuplicates.add(photo.duplicateGroup);
      const members = byDuplicate.get(photo.duplicateGroup)!;
      const representative =
        members.find((member) => member.id === currentId) ??
        members.find((member) => member.selected) ??
        members.reduce((best, member) =>
          (member.rankScore ?? 0) > (best.rankScore ?? 0) ? member : best,
        );
      result.push(representative);
    } else if (photo.burstId != null && byBurst.has(photo.burstId)) {
      if (seenBursts.has(photo.burstId)) continue;
      seenBursts.add(photo.burstId);
      const members = byBurst.get(photo.burstId)!;
      const representative =
        members.find((member) => member.id === currentId) ??
        members.find((member) => member.selected) ??
        members.find((member) => member.burstRank === 1) ??
        members[0];
      result.push(representative);
    } else {
      result.push(photo);
    }
  }
  return result;
}

export default function App() {
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState<string | null>(null);
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [activeUserId, setActiveUserId] = useState<number | null>(null);
  const [showProfiles, setShowProfiles] = useState(false);
  const [newProfileName, setNewProfileName] = useState("");
  const [profileError, setProfileError] = useState<string | null>(null);
  const [preferencesSummary, setPreferencesSummary] = useState<PreferencesSummary | null>(null);
  const lastGridIndex = useRef(0);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [data, setData] = useState<PhotosResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [job, setJob] = useState<Job | null>(null);
  const [copyJob, setCopyJob] = useState<Job | null>(null);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [expandedDuplicates, setExpandedDuplicates] = useState<Set<number>>(new Set());
  const [focusedId, setFocusedId] = useState<number | null>(null);
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const [loupeId, setLoupeId] = useState<number | null>(null);
  const [loupeZoom, setLoupeZoom] = useState<"fit" | "fit15" | "100" | "200">("fit");
  const [loupeShowStrip, setLoupeShowStrip] = useState(true);
  const [loupeSequence, setLoupeSequence] = useState<Photo[]>([]);
  const [loupeSimilarGroupIds, setLoupeSimilarGroupIds] = useState<number[]>([]);
  const [reviewedSimilarGroups, setReviewedSimilarGroups] = useState<Set<number>>(new Set());
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [copySummary, setCopySummary] = useState<CopyResult | null>(null);
  const [copyStarting, setCopyStarting] = useState(false);
  const [creatingSource, setCreatingSource] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [undoStack, setUndoStack] = useState<UndoEntry[]>([]);
  const [targetInput, setTargetInput] = useState("");

  const reload = useCallback(() => setReloadToken((value) => value + 1), []);

  const activeUser = users.find((user) => user.id === activeUserId) ?? null;

  const resetReviewState = useCallback(() => {
    setData(null);
    setFilter("all");
    setExpanded(new Set());
    setExpandedDuplicates(new Set());
    setFocusedId(null);
    setHoveredId(null);
    setLoupeId(null);
    setCopySummary(null);
    setUndoStack([]);
    setTargetInput("");
    setPreferencesSummary(null);
  }, []);

  const switchUser = useCallback(
    async (user: User) => {
      setActiveUserId(user.id);
      window.localStorage.setItem("topshot.activeUser", String(user.id));
      setShowProfiles(false);
      setProfileError(null);
      setUsers((current) =>
        current.map((item) =>
          item.id === user.id ? { ...item, lastActiveAt: new Date().toISOString() } : item,
        ),
      );
      resetReviewState();
      try {
        await rpc("users.activate", { userId: user.id });
        const list = await rpc<{ projects: Project[] }>("project.list", { userId: user.id });
        setProjects(list.projects);
        setProjectId((current) =>
          list.projects.some((project) => project.id === current)
            ? current
            : (list.projects[0]?.id ?? null),
        );
        setReloadToken((value) => value + 1);
      } catch (error) {
        setToast(readError(error));
      }
    },
    [resetReviewState],
  );

  const createProfile = async () => {
    const name = newProfileName.trim();
    if (!name) {
      setProfileError("Enter a profile name.");
      return;
    }
    try {
      const response = await rpc<{ user: User }>("users.create", { name });
      setUsers((current) => [...current, response.user]);
      setNewProfileName("");
      setProfileError(null);
      await switchUser(response.user);
    } catch (error) {
      setProfileError(readError(error));
    }
  };

  const renameProfile = async () => {
    if (!activeUser) return;
    const name = window.prompt("Rename profile", activeUser.name);
    if (!name || name.trim() === activeUser.name) return;
    try {
      const response = await rpc<{ user: User }>("users.update", {
        userId: activeUser.id,
        name: name.trim(),
      });
      setUsers((current) =>
        current.map((item) => (item.id === activeUser.id ? { ...item, ...response.user } : item)),
      );
    } catch (error) {
      setProfileError(readError(error));
    }
  };

  const deleteProfile = async () => {
    if (!activeUser) return;
    if (users.length <= 1) {
      setProfileError("The last profile cannot be deleted.");
      return;
    }
    if (!window.confirm(`Delete profile “${activeUser.name}” and its selections and preferences?`)) {
      return;
    }
    try {
      await rpc("users.delete", { userId: activeUser.id });
      const list = await rpc<{ users: User[] }>("users.list");
      setUsers(list.users);
      const fallback = list.users[0];
      if (fallback) await switchUser(fallback);
    } catch (error) {
      setProfileError(readError(error));
    }
  };

  const resetPreferences = async () => {
    if (!activeUser || !data) return;
    if (!window.confirm("Reset this profile's learned preferences? The AI will return to the baseline ranking.")) {
      return;
    }
    try {
      const response = await rpc<{ preferences: PreferencesSummary }>("preferences.reset", {
        userId: activeUser.id,
        projectId: data.project.id,
      });
      setPreferencesSummary(response.preferences);
      reload();
      setToast("Personalization reset for this profile.");
    } catch (error) {
      setToast(readError(error));
    }
  };


  useEffect(() => {
    (async () => {
      try {
        await initEngine();
        const info = await rpc<AppInfo>("app.info");
        setAppInfo(info);
        const profiles = await rpc<{ users: User[] }>("users.list");
        setUsers(profiles.users);
        const remembered = Number(window.localStorage.getItem("topshot.activeUser") || 0);
        const active =
          profiles.users.find((user) => user.id === remembered) ?? profiles.users[0] ?? null;
        if (active) {
          setActiveUserId(active.id);
          window.localStorage.setItem("topshot.activeUser", String(active.id));
          await rpc("users.activate", { userId: active.id });
        }
        const list = await rpc<{ projects: Project[] }>("project.list", {
          userId: active?.id ?? 1,
        });
        setProjects(list.projects);
        if (list.projects.length > 0) setProjectId(list.projects[0].id);
      } catch (error) {
        setBootError(readError(error));
      } finally {
        setBooting(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (projectId == null) return;
    let cancelled = false;
    setLoading(true);
    rpc<PhotosResponse>("photos.list", { projectId, userId: activeUserId })
      .then((response) => {
        if (cancelled) return;
        setData(response);
        setProjects((current) => {
          const rest = current.filter((project) => project.id !== response.project.id);
          return [response.project, ...rest].sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1));
        });
      })
      .catch((error) => {
        if (!cancelled) setToast(readError(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeUserId, projectId, reloadToken]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 5200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    if (!data) return;
    if (focusedId == null || !data.photos.some((photo) => photo.id === focusedId)) {
      setFocusedId(data.photos[0]?.id ?? null);
    }
  }, [data, focusedId]);

  useEffect(() => {
    setTargetInput(data?.project.keepTarget ? String(data.project.keepTarget) : "");
  }, [data?.project.id, data?.project.keepTarget]);

  useEffect(() => {
    if (activeUserId == null) return;
    let cancelled = false;
    rpc<{ preferences: PreferencesSummary }>("preferences.summary", { userId: activeUserId })
      .then((response) => {
        if (!cancelled) setPreferencesSummary(response.preferences);
      })
      .catch(() => {
        if (!cancelled) setPreferencesSummary(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeUserId, reloadToken]);

  useEffect(() => {
    if (loupeId == null || !data) return;
    if (!data.photos.some((photo) => photo.id === loupeId)) setLoupeId(null);
  }, [data, loupeId]);

  // Pipeline job polling.
  useEffect(() => {
    const id = job?.id;
    if (!id || (job.status !== "running" && job.status !== "queued")) return;
    let stopped = false;
    const poll = async () => {
      try {
        const response = await rpc<{ job: Job }>("job.status", { jobId: id });
        if (stopped) return;
        setJob(response.job);
        if (response.job.status === "done") {
          const scan = response.job.result?.scan;
          reload();
          setToast(
            scan
              ? `Import complete · ${formatNumber(scan.supported)} photos · ${formatNumber(scan.videos)} videos skipped`
              : "Analysis complete",
          );
          window.setTimeout(() => setJob(null), 500);
        } else if (response.job.status === "canceled") {
          reload();
          setToast("Analysis canceled — progress is saved. Resume any time.");
          setJob(null);
        }
      } catch (error) {
        if (!stopped) {
          setToast(readError(error));
          setJob(null);
        }
      }
    };
    const timer = window.setInterval(poll, 650);
    poll();
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [job?.id, job?.status, reload]);

  // Copy job polling.
  useEffect(() => {
    const id = copyJob?.id;
    if (!id || (copyJob.status !== "running" && copyJob.status !== "queued")) return;
    let stopped = false;
    const poll = async () => {
      try {
        const response = await rpc<{ job: Job }>("job.status", { jobId: id });
        if (stopped) return;
        setCopyJob(response.job);
        if (response.job.status === "done" || response.job.status === "canceled") {
          const raw = response.job.result?.copy ?? null;
          const result = raw && "counts" in raw ? raw : null;
          if (result) setCopySummary(result);
          reload();
          setCopyJob(null);
          setToast(
            result?.status === "canceled"
              ? "Copy canceled — completed files were kept."
              : `Copy complete · ${formatNumber(result?.counts.copied ?? 0)} files verified`,
          );
        }
      } catch (error) {
        if (!stopped) {
          setToast(readError(error));
          setCopyJob(null);
        }
      }
    };
    const timer = window.setInterval(poll, 650);
    poll();
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [copyJob?.id, copyJob?.status, reload]);

  const selectProject = (id: number) => {
    if (id === projectId) return;
    setProjectId(id);
    setData(null);
    setFilter("all");
    setExpanded(new Set());
    setExpandedDuplicates(new Set());
    setFocusedId(null);
    setHoveredId(null);
    setLoupeId(null);
    setCopySummary(null);
    setUndoStack([]);
    setTargetInput("");
  };

  const createProject = useCallback(async (sourceDir: string) => {
    setCreatingSource(true);
    try {
      const response = await rpc<{ project: Project; job: Job | null; existing: boolean }>("project.create", {
        sourceDir,
        userId: activeUserId ?? 1,
      });
      setProjects((current) => {
        const rest = current.filter((project) => project.id !== response.project.id);
        return [response.project, ...rest];
      });
      setProjectId(response.project.id);
      setData(null);
      setFilter("all");
      setExpanded(new Set());
      setExpandedDuplicates(new Set());
      setFocusedId(null);
    setHoveredId(null);
    setLoupeId(null);
    setLoupeSequence([]);
    setLoupeSimilarGroupIds([]);
    setReviewedSimilarGroups(new Set());
    setCopySummary(null);
      setUndoStack([]);
      setTargetInput("");
      if (response.job) setJob(response.job);
      else setToast(response.existing ? "Project already imported." : "Project opened.");
    } catch (error) {
      setToast(readError(error));
    } finally {
      setCreatingSource(false);
    }
  }, []);

  const chooseSource = async () => {
    try {
      const folder = await pickFolder("Choose the shoot folder to import");
      if (folder) await createProject(folder);
    } catch (error) {
      setToast(readError(error));
    }
  };

  useEffect(() => {
    if (!isTauri()) return;
    let unlisten: (() => void) | undefined;
    let disposed = false;
    listenForFolderDrop(
      (paths) => {
        const path = paths[0];
        if (!path) return;
        if (/\.(mov|mp4|m4v|avi|mkv|webm|3gp|mts|m2ts|jpe?g|heic|heif|cr[23]|nef|arw|raf|dng|orf|rw2)$/i.test(path)) {
          setToast("Drop a folder, not a single file.");
          return;
        }
        void createProject(path);
      },
      setDragOver,
    )
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch((error) => setToast(readError(error)));
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [createProject]);

  const filteredPhotos = useMemo(() => {
    if (!data) return [];
    if (filter === "all") return data.photos;
    if (filter === "selected") return data.photos.filter((photo) => photo.selected);
    if (filter === "people") return data.photos.filter((photo) => photo.peopleCount > 0);
    if (filter === "duplicates") return data.photos.filter((photo) => photo.duplicateGroup != null);
    if (filter === "docs") return data.photos.filter((photo) => (photo.documentationScore ?? 0) >= 0.45);
    return data.photos.filter((photo) => photo.effectiveState === filter);
  }, [data, filter]);

  const navigablePhotos = useMemo(() => {
    if (!data) return [];
    if (filter !== "all") return filteredPhotos;
    const byBurst = new Map<number, Photo[]>();
    const byDuplicate = new Map<number, Photo[]>();
    for (const photo of filteredPhotos) {
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
    const result: Photo[] = [];
    const seenBursts = new Set<number>();
    const seenDuplicates = new Set<number>();
    for (const photo of filteredPhotos) {
      if (photo.duplicateGroup != null && byDuplicate.has(photo.duplicateGroup)) {
        if (seenDuplicates.has(photo.duplicateGroup)) continue;
        seenDuplicates.add(photo.duplicateGroup);
        const members = byDuplicate.get(photo.duplicateGroup)!;
        if (expandedDuplicates.has(photo.duplicateGroup)) result.push(...members);
        else
          result.push(
            members.reduce((best, member) =>
              (member.rankScore ?? 0) > (best.rankScore ?? 0) ? member : best,
            ),
          );
      } else if (photo.burstId != null && byBurst.has(photo.burstId)) {
        if (seenBursts.has(photo.burstId)) continue;
        seenBursts.add(photo.burstId);
        const members = byBurst.get(photo.burstId)!;
        if (expanded.has(photo.burstId)) result.push(...members);
        else result.push(members.find((member) => member.burstRank === 1) ?? members[0]);
      } else {
        result.push(photo);
      }
    }
    return result;
  }, [data, filteredPhotos, filter, expanded, expandedDuplicates]);

  const focusedPhoto = data?.photos.find((photo) => photo.id === focusedId) ?? null;
  const loupePhoto = data?.photos.find((photo) => photo.id === loupeId) ?? null;
  const loupeGroup = useMemo(() => {
    if (!loupePhoto || !data) {
      return { members: [] as Photo[], kind: null as "duplicate" | "burst" | null, id: null as number | null };
    }
    if (loupePhoto.duplicateGroup != null && (loupePhoto.duplicateSize ?? 0) > 1) {
      return {
        members: data.photos
          .filter((photo) => photo.duplicateGroup === loupePhoto.duplicateGroup)
          .sort((a, b) => (b.rankScore ?? 0) - (a.rankScore ?? 0)),
        kind: "duplicate" as const,
        id: loupePhoto.duplicateGroup,
      };
    }
    if (loupePhoto.burstId != null && (loupePhoto.burstSize ?? 0) > 1) {
      return {
        members: data.photos
          .filter((photo) => photo.burstId === loupePhoto.burstId)
          .sort((a, b) => (a.burstRank ?? 99) - (b.burstRank ?? 99)),
        kind: "burst" as const,
        id: loupePhoto.burstId,
      };
    }
    return { members: [] as Photo[], kind: null as "duplicate" | "burst" | null, id: null as number | null };
  }, [loupePhoto, data]);
  // The loupe loops the filter set that was on screen when it opened. The
  // sequence is snapshotted so edits (for example unselecting the current
  // photo in a Selected filter) do not drop the current group and freeze
  // left/right navigation.
  const loupeGroupSequence = useMemo(
    () => (loupeSequence.length > 0 ? loupeSequence : loupePhoto ? [loupePhoto] : []),
    [loupeSequence, loupePhoto],
  );
  const loupeGroupIndex = useMemo(() => {
    if (!loupePhoto) return -1;
    return loupeGroupSequence.findIndex(
      (photo) =>
        photo.id === loupePhoto.id ||
        (photo.duplicateGroup != null && photo.duplicateGroup === loupePhoto.duplicateGroup) ||
        (photo.burstId != null && photo.burstId === loupePhoto.burstId),
    );
  }, [loupeGroupSequence, loupePhoto]);

  useEffect(() => {
    setReviewedSimilarGroups(new Set());
    setLoupeSequence([]);
    setLoupeSimilarGroupIds([]);
  }, [filter, data?.project.id, activeUserId]);

  useEffect(() => {
    if (!loupePhoto) return;
    const groupId = loupePhoto.duplicateGroup;
    if (groupId == null || (loupePhoto.duplicateSize ?? 0) <= 1) return;
    if (!loupeSimilarGroupIds.includes(groupId)) return;
    setReviewedSimilarGroups((current) => {
      if (current.has(groupId)) return current;
      const next = new Set(current);
      next.add(groupId);
      return next;
    });
  }, [loupePhoto, loupeSimilarGroupIds]);

  const similarGroupsRemaining = useMemo(
    () =>
      loupeSimilarGroupIds.reduce(
        (remaining, groupId) => remaining + (reviewedSimilarGroups.has(groupId) ? 0 : 1),
        0,
      ),
    [loupeSimilarGroupIds, reviewedSimilarGroups],
  );

  useEffect(() => {
    const targetId = hoveredId ?? focusedId;
    if (targetId == null) return;
    const index = navigablePhotos.findIndex((photo) => photo.id === targetId);
    if (index >= 0) lastGridIndex.current = index;
  }, [focusedId, hoveredId, navigablePhotos]);

  const pushUndo = useCallback((entry: UndoEntry) => {
    setUndoStack((current) => [...current.slice(-49), entry]);
  }, []);

  const updatePhoto = useCallback(
    async (photo: Photo, patch: { selected?: boolean; decision?: Decision; rating?: number | null }, note?: string) => {
      try {
        const response = await rpc<PhotoUpdateResponse>("photo.update", {
          fileId: photo.id,
          userId: activeUserId ?? 1,
          ...patch,
        });
        setData((current) =>
          current
            ? {
                ...current,
                photos: current.photos.map((item) => (item.id === photo.id ? response.photo : item)),
                counts: response.counts,
              }
            : current,
        );
        pushUndo({
          kind: "photo",
          photoId: photo.id,
          before: { selected: photo.selected, decision: photo.decision, rating: photo.rating },
          label: note ?? "Edit",
        });
        if (note) setToast(note);
      } catch (error) {
        setToast(readError(error));
      }
    },
    [pushUndo],
  );

  const toggleSelect = useCallback(
    (photo: Photo) => {
      void updatePhoto(photo, { selected: !photo.selected });
    },
    [updatePhoto],
  );

  const rejectPhoto = useCallback(
    (photo: Photo) => {
      void updatePhoto(photo, { decision: "reject" }, "Excluded from copy. Original untouched.");
    },
    [updatePhoto],
  );

  const ratePhoto = useCallback(
    (photo: Photo, rating: number) => {
      void updatePhoto(photo, { rating }, `Rated ${"★".repeat(rating)} — selected for copy.`);
    },
    [updatePhoto],
  );

  const unratePhoto = useCallback(
    (photo: Photo) => {
      void updatePhoto(photo, { rating: null, decision: "none" }, "Rating cleared.");
    },
    [updatePhoto],
  );

  const undo = useCallback(async () => {
    const entry = undoStack[undoStack.length - 1];
    if (!entry || !data) return;
    setUndoStack((current) => current.slice(0, -1));
    try {
      if (entry.kind === "group") {
        const response = await rpc<{ photos: Photo[]; counts: Counts }>("group.pick", {
          projectId: data.project.id,
          userId: activeUserId ?? 1,
          kind: entry.groupType,
          groupId: entry.groupId,
          fileId: entry.previousId,
        });
        setData((current) => {
          if (!current) return current;
          const updates = new Map(response.photos.map((photo) => [photo.id, photo]));
          return {
            ...current,
            photos: current.photos.map((photo) => updates.get(photo.id) ?? photo),
            counts: response.counts,
          };
        });
      } else {
        const response = await rpc<PhotoUpdateResponse>("photo.update", {
          fileId: entry.photoId,
          userId: activeUserId ?? 1,
          selected: entry.before.selected,
          decision: entry.before.decision,
          rating: entry.before.rating,
        });
        setData((current) =>
          current
            ? {
                ...current,
                photos: current.photos.map((item) => (item.id === entry.photoId ? response.photo : item)),
                counts: response.counts,
              }
            : current,
        );
      }
      setToast("Undone");
    } catch (error) {
      setToast(readError(error));
    }
  }, [undoStack, data]);

  const toggleBurst = useCallback(
    (burstId: number) => {
      setExpanded((current) => {
        const next = new Set(current);
        if (next.has(burstId)) {
          next.delete(burstId);
          const members = data?.photos.filter((photo) => photo.burstId === burstId) ?? [];
          if (members.some((photo) => photo.id === focusedId)) {
            const cover = members.find((photo) => photo.burstRank === 1) ?? members[0];
            if (cover) setFocusedId(cover.id);
          }
        } else {
          next.add(burstId);
        }
        return next;
      });
    },
    [data, focusedId],
  );

  const toggleDuplicate = useCallback(
    (groupId: number) => {
      setExpandedDuplicates((current) => {
        const next = new Set(current);
        if (next.has(groupId)) {
          next.delete(groupId);
          const members = data?.photos.filter((photo) => photo.duplicateGroup === groupId) ?? [];
          if (members.some((photo) => photo.id === focusedId)) {
            const cover = members.reduce((best, member) =>
              (member.rankScore ?? 0) > (best.rankScore ?? 0) ? member : best,
            );
            if (cover) setFocusedId(cover.id);
          }
        } else {
          next.add(groupId);
        }
        return next;
      });
    },
    [data, focusedId],
  );

  const openLoupe = useCallback(
    (id: number) => {
      const source = filter === "all" ? (data?.photos ?? []) : filteredPhotos;
      const sequence = buildGroupSequence(source, id);
      const groupIds = new Set<number>();
      for (const photo of source) {
        if (photo.duplicateGroup != null && (photo.duplicateSize ?? 0) > 1) {
          groupIds.add(photo.duplicateGroup);
        }
      }
      setLoupeSequence(sequence);
      setLoupeSimilarGroupIds([...groupIds]);
      setFocusedId(id);
      setLoupeId(id);
      setLoupeZoom("fit");
      setLoupeShowStrip(true);
    },
    [data, filter, filteredPhotos],
  );

  const swapGroupPhoto = useCallback(
    async (photo: Photo, kind: "duplicate" | "burst", groupId: number) => {
      if (!data) return;
      const members = data.photos.filter((item) =>
        kind === "duplicate" ? item.duplicateGroup === groupId : item.burstId === groupId,
      );
      const previous = members.find((item) => item.selected) ?? null;
      try {
        const response = await rpc<{ photos: Photo[]; counts: Counts }>("group.pick", {
          projectId: data.project.id,
          userId: activeUserId ?? 1,
          kind,
          groupId,
          fileId: photo.id,
        });
        setData((current) => {
          if (!current) return current;
          const updates = new Map(response.photos.map((item) => [item.id, item]));
          return {
            ...current,
            photos: current.photos.map((item) => updates.get(item.id) ?? item),
            counts: response.counts,
          };
        });
        pushUndo({
          kind: "group",
          groupId,
          groupType: kind,
          previousId: previous?.id ?? null,
          label: kind === "duplicate" ? "Swap similar pick" : "Swap burst pick",
        });
        setToast(kind === "duplicate" ? "Similar group pick updated." : "Burst pick updated.");
      } catch (error) {
        setToast(readError(error));
      }
    },
    [data, pushUndo],
  );

  const nextLoupeZoom = useCallback(() => {
    setLoupeZoom((value) =>
      value === "fit" ? "fit15" : value === "fit15" ? "100" : value === "100" ? "200" : "fit",
    );
  }, []);

  const navigateGroup = useCallback(
    (delta: number) => {
      if (loupeGroupIndex < 0 || loupeGroupSequence.length === 0) return;
      const next = (loupeGroupIndex + delta + loupeGroupSequence.length) % loupeGroupSequence.length;
      setLoupeId(loupeGroupSequence[next].id);
      setFocusedId(loupeGroupSequence[next].id);
    },
    [loupeGroupIndex, loupeGroupSequence],
  );

  const navigateAlternative = useCallback(
    (delta: number) => {
      if (loupeGroup.members.length < 2) return;
      const index = loupeGroup.members.findIndex((member) => member.id === loupePhoto?.id);
      if (index < 0) return;
      const next = (index + delta + loupeGroup.members.length) % loupeGroup.members.length;
      setLoupeId(loupeGroup.members[next].id);
      setFocusedId(loupeGroup.members[next].id);
    },
    [loupeGroup.members, loupePhoto?.id],
  );

  // Single keyboard owner for grid and loupe. In the grid the hovered tile is
  // the target; in the loupe the displayed frame is the target.
  useEffect(() => {
    if (preflight || copySummary) return;
    const handler = (event: KeyboardEvent) => {
      const targetElement = event.target as HTMLElement | null;
      if (
        targetElement &&
        (["INPUT", "SELECT", "TEXTAREA"].includes(targetElement.tagName) || targetElement.isContentEditable)
      ) {
        return;
      }
      if (data == null) return;
      const modifier = event.metaKey || event.ctrlKey;

      if (modifier && event.key.toLowerCase() === "z") {
        event.preventDefault();
        void undo();
        return;
      }
      if (modifier) return;

      if (loupeId != null) {
        const photo = loupePhoto;
        if (!photo) return;
        if (event.key === "Escape") {
          event.preventDefault();
          setLoupeId(null);
          return;
        }
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          navigateGroup(-1);
          return;
        }
        if (event.key === "ArrowRight") {
          event.preventDefault();
          navigateGroup(1);
          return;
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          navigateAlternative(-1);
          return;
        }
        if (event.key === "ArrowDown") {
          event.preventDefault();
          navigateAlternative(1);
          return;
        }
        if (event.key === " " || event.key.toLowerCase() === "f") {
          event.preventDefault();
          nextLoupeZoom();
          return;
        }
        if (event.key.toLowerCase() === "s") {
          event.preventDefault();
          toggleSelect(photo);
          return;
        }
        if (event.key.toLowerCase() === "x") {
          event.preventDefault();
          rejectPhoto(photo);
          return;
        }
        if (event.key.toLowerCase() === "u") {
          event.preventDefault();
          unratePhoto(photo);
          return;
        }
        if (event.key.toLowerCase() === "b") {
          if (loupeGroup.kind != null && loupeGroup.id != null) {
            event.preventDefault();
            setLoupeShowStrip((value) => !value);
          }
          return;
        }
        if (event.key === "Enter") {
          if (loupeGroup.kind != null && loupeGroup.id != null && !photo.selected) {
            event.preventDefault();
            void swapGroupPhoto(photo, loupeGroup.kind, loupeGroup.id);
          }
          return;
        }
        if (/^[1-5]$/.test(event.key)) {
          event.preventDefault();
          ratePhoto(photo, Number(event.key));
        }
        return;
      }

      if (navigablePhotos.length === 0) return;
      const targetId = hoveredId ?? focusedId;
      const foundIndex = navigablePhotos.findIndex((photo) => photo.id === targetId);
      const targetMissing = foundIndex < 0;
      const currentIndex = targetMissing
        ? Math.min(lastGridIndex.current, Math.max(0, navigablePhotos.length - 1))
        : foundIndex;
      if (!targetMissing) lastGridIndex.current = foundIndex;
      let nextIndex: number | null = null;
      if (event.key === "ArrowRight") {
        nextIndex = targetMissing
          ? Math.min(navigablePhotos.length - 1, currentIndex)
          : Math.min(navigablePhotos.length - 1, currentIndex + 1);
      }
      if (event.key === "ArrowLeft") nextIndex = Math.max(0, currentIndex - 1);
      if (event.key === "ArrowDown") nextIndex = Math.min(navigablePhotos.length - 1, currentIndex + 5);
      if (event.key === "ArrowUp") nextIndex = Math.max(0, currentIndex - 5);
      if (nextIndex != null) {
        event.preventDefault();
        setHoveredId(null);
        const photo = navigablePhotos[nextIndex];
        lastGridIndex.current = nextIndex;
        setFocusedId(photo.id);
        document.querySelector<HTMLElement>(`[data-photo-id="${photo.id}"]`)?.scrollIntoView({ block: "nearest" });
        return;
      }
      const photo = navigablePhotos.find((item) => item.id === targetId) ?? focusedPhoto;
      if (!photo) return;
      if (event.key === " ") {
        event.preventDefault();
        openLoupe(photo.id);
      } else if (event.key.toLowerCase() === "s") {
        event.preventDefault();
        toggleSelect(photo);
      } else if (event.key.toLowerCase() === "x") {
        event.preventDefault();
        rejectPhoto(photo);
      } else if (event.key.toLowerCase() === "u") {
        event.preventDefault();
        unratePhoto(photo);
      } else if (event.key.toLowerCase() === "b") {
        if (photo.duplicateGroup != null && (photo.duplicateSize ?? 0) > 1) {
          event.preventDefault();
          toggleDuplicate(photo.duplicateGroup);
        } else if (photo.burstId != null) {
          event.preventDefault();
          toggleBurst(photo.burstId);
        }
      } else if (/^[1-5]$/.test(event.key)) {
        event.preventDefault();
        ratePhoto(photo, Number(event.key));
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [
    copySummary,
    data,
    focusedId,
    focusedPhoto,
    hoveredId,
    loupeGroup.id,
    loupeGroup.kind,
    loupeId,
    loupePhoto,
    navigablePhotos,
    navigateAlternative,
    navigateGroup,
    nextLoupeZoom,
    openLoupe,
    preflight,
    ratePhoto,
    rejectPhoto,
    swapGroupPhoto,
    toggleBurst,
    toggleDuplicate,
    toggleSelect,
    undo,
    unratePhoto,
  ]);

  const updateProjectSetting = async (patch: Record<string, unknown>) => {
    if (!data) return;
    try {
      const response = await rpc<{ project: Project }>("project.update", {
        projectId: data.project.id,
        userId: activeUserId ?? 1,
        ...patch,
      });
      setData((current) => (current ? { ...current, project: response.project } : current));
      setProjects((current) => current.map((item) => (item.id === response.project.id ? response.project : item)));
      reload();
    } catch (error) {
      setToast(readError(error));
    }
  };

  const applyKeepTarget = async (target: number | null) => {
    if (!data) return;
    try {
      const response = await rpc<{ project: Project }>("project.update", {
        projectId: data.project.id,
        userId: activeUserId ?? 1,
        keepTarget: target,
      });
      setData((current) => (current ? { ...current, project: response.project } : current));
      setProjects((current) => current.map((item) => (item.id === response.project.id ? response.project : item)));
      reload();
      setToast(
        target
          ? `Keep target set to the top ${formatNumber(target)} photos. AI picks updated.`
          : "Keep target cleared — all AI-chosen keeps are selected again.",
      );
    } catch (error) {
      setToast(readError(error));
    }
  };

  const commitKeepTarget = () => {
    const raw = targetInput.trim();
    if (!raw) {
      void applyKeepTarget(null);
      return;
    }
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 1) {
      setToast("Enter a positive number of photos to keep.");
      return;
    }
    void applyKeepTarget(Math.min(Math.floor(value), data?.project.photoCount ?? Math.floor(value)));
  };

  const clearKeepTarget = () => {
    setTargetInput("");
    void applyKeepTarget(null);
  };

  const chooseDestination = async (): Promise<string | null> => {
    if (!data) return null;
    try {
      const folder = await pickFolder("Choose the destination folder for confirmed keeps");
      if (!folder) return null;
      await updateProjectSetting({ destinationDir: folder });
      return folder;
    } catch (error) {
      setToast(readError(error));
      return null;
    }
  };

  const openCopyDialog = async () => {
    if (!data) return;
    const selectedIds = data.photos.filter((photo) => photo.selected).map((photo) => photo.id);
    if (selectedIds.length === 0) {
      setToast("Nothing selected. Check the keeps you want to copy.");
      return;
    }
    try {
      let destination = data.project.destinationDir;
      if (!destination) {
        destination = await chooseDestination();
        if (!destination) return;
      }
      const result = await rpc<Preflight>("copy.preflight", {
        projectId: data.project.id,
        userId: activeUserId ?? 1,
        fileIds: selectedIds,
        destination,
      });
      setPreflight(result);
    } catch (error) {
      setToast(readError(error));
    }
  };

  const confirmCopy = async () => {
    if (!data || !preflight) return;
    const selectedIds = data.photos.filter((photo) => photo.selected).map((photo) => photo.id);
    setCopyStarting(true);
    try {
      const response = await rpc<{ job: Job }>("copy.run", {
        projectId: data.project.id,
        userId: activeUserId ?? 1,
        fileIds: selectedIds,
        destination: preflight.destination,
      });
      setPreflight(null);
      const job = response.job;
      if (job.status === "done" || job.status === "canceled") {
        const result = job.result?.copy ?? null;
        if (result && "counts" in result) setCopySummary(result);
        reload();
      } else {
        setCopyJob(job);
      }
    } catch (error) {
      setToast(readError(error));
    } finally {
      setCopyStarting(false);
    }
  };

  const handleOpenDestination = async () => {
    if (!copySummary) return;
    try {
      await openPath(copySummary.destination);
    } catch (error) {
      setToast(readError(error));
    }
  };

  const resumeAnalysis = async () => {
    if (!data) return;
    try {
      const response = await rpc<{ job: Job }>("project.rescan", {
        projectId: data.project.id,
        userId: activeUserId ?? 1,
      });
      setJob(response.job);
    } catch (error) {
      setToast(readError(error));
    }
  };

  const cancelJob = async (id: string) => {
    try {
      await rpc("job.cancel", { jobId: id });
    } catch (error) {
      setToast(readError(error));
    }
  };

  const pipelineBusy = job?.status === "running" || job?.status === "queued";
  const copyBusy = copyJob?.status === "running" || copyJob?.status === "queued";
  const busy = pipelineBusy || copyBusy || creatingSource || copyStarting;

  const currentStep = !data ? 0 : pipelineBusy || data.project.status === "interrupted" ? 1 : copySummary ? 3 : 2;
  const scan: ScanStats | null = data?.project.lastScan ?? null;

  if (booting) {
    return (
      <div className="splash">
        <div className="brand-mark large" aria-hidden="true">
          <span />
        </div>
        <h1>Topshot</h1>
        <p className="muted">Starting the local engine…</p>
      </div>
    );
  }

  if (bootError) {
    return (
      <div className="splash">
        <h1>Engine unavailable</h1>
        <p className="muted">{bootError}</p>
        <button type="button" className="primary" onClick={() => window.location.reload()}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className={`app ${dragOver ? "drag-over" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <span />
          </div>
          <div>
            <strong>Topshot</strong>
            <span className="muted">local cull</span>
          </div>
        </div>

        <div className="profile-switcher">
          <button
            type="button"
            className="profile-button"
            onClick={() => {
              setShowProfiles((value) => !value);
              setProfileError(null);
            }}
            aria-expanded={showProfiles}
            aria-haspopup="menu"
          >
            <span
              className="profile-avatar"
              style={{ background: activeUser?.color ?? "var(--accent)" }}
              aria-hidden="true"
            >
              {(activeUser?.name ?? "?").slice(0, 2).toUpperCase()}
            </span>
            <span className="profile-button-text">
              <span className="muted">Reviewing as</span>
              <strong>{activeUser?.name ?? "Profile"}</strong>
            </span>
            <span className="profile-chevron" aria-hidden="true">
              ⌄
            </span>
          </button>
          {showProfiles && (
            <>
              <button
                type="button"
                className="profile-backdrop"
                aria-label="Close profile menu"
                onClick={() => setShowProfiles(false)}
              />
              <div className="profile-menu" role="menu">
                <span className="eyebrow">Profiles</span>
                {users.map((user) => (
                  <button
                    type="button"
                    key={user.id}
                    className={`profile-option ${user.id === activeUser?.id ? "active" : ""}`}
                    onClick={() => void switchUser(user)}
                    role="menuitem"
                  >
                    <span
                      className="profile-avatar small"
                      style={{ background: user.color ?? "var(--accent)" }}
                      aria-hidden="true"
                    >
                      {user.name.slice(0, 2).toUpperCase()}
                    </span>
                    <span>
                      <strong>{user.name}</strong>
                    </span>
                    {user.id === activeUser?.id && <span className="profile-check">✓</span>}
                  </button>
                ))}
                <form
                  className="profile-create"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void createProfile();
                  }}
                >
                  <input
                    value={newProfileName}
                    onChange={(event) => setNewProfileName(event.target.value)}
                    placeholder="New profile name"
                    aria-label="New profile name"
                    maxLength={40}
                  />
                  <button type="submit" className="primary small">
                    Add
                  </button>
                </form>
                {profileError && <span className="warn-text">{profileError}</span>}
                <div className="profile-actions">
                  <button type="button" className="ghost small" onClick={() => void renameProfile()}>
                    Rename
                  </button>
                  <button type="button" className="danger-ghost small" onClick={() => void deleteProfile()}>
                    Delete
                  </button>
                </div>
              </div>
            </>
          )}
        </div>

        <button type="button" className="primary full" onClick={chooseSource} disabled={busy}>
          + New Shoot
        </button>

        <nav className="project-list" aria-label="Projects">
          {projects.length === 0 && <p className="muted small">No projects yet.</p>}
          {projects.map((project) => (
            <button
              type="button"
              key={project.id}
              className={`project-item ${project.id === projectId ? "active" : ""}`}
              onClick={() => !busy && selectProject(project.id)}
              disabled={busy}
            >
              <span className={`status-dot status-${project.status}`} aria-hidden="true" />
              <span className="project-text">
                <strong>{project.name}</strong>
                <span className="muted">
                  {formatNumber(project.photoCount)} photos · {basename(project.sourceDir)}
                </span>
              </span>
            </button>
          ))}
        </nav>

        {data && (
          <section className="settings">
            <h3>Shoot settings</h3>
            <label>
              <span>RAW+JPEG</span>
              <select
                value={data.project.rawJpegMode}
                onChange={(event) => void updateProjectSetting({ rawJpegMode: event.target.value })}
                disabled={busy}
              >
                <option value="both">Copy both as a pair</option>
                <option value="raw">RAW only</option>
                <option value="jpeg">JPEG only</option>
              </select>
            </label>
            <label>
              <span>Aggressiveness</span>
              <select
                value={data.project.preset}
                onChange={(event) => void updateProjectSetting({ preset: event.target.value })}
                disabled={busy}
              >
                <option value="conservative">Conservative (default)</option>
                <option value="balanced">Balanced</option>
                <option value="aggressive">Aggressive</option>
              </select>
            </label>
            <div className="setting-block">
              <span className="setting-label">Destination</span>
              <span className="path" title={data.project.destinationDir ?? ""}>
                {data.project.destinationDir ?? "Not chosen"}
              </span>
              <div className="setting-actions">
                <button type="button" className="ghost small" onClick={() => void chooseDestination()} disabled={busy}>
                  Choose…
                </button>
                <button
                  type="button"
                  className="ghost small"
                  onClick={() => void resumeAnalysis()}
                  disabled={busy}
                  title="Scan again for new photos; existing decisions are preserved"
                >
                  Rescan
                </button>
              </div>
            </div>
            <div className="setting-block">
              <span className="setting-label">Source</span>
              <span className="path" title={data.project.sourceDir}>
                {shortPath(data.project.sourceDir, 3)}
              </span>
              <span className="fineprint">Read-only. Topshot never writes here.</span>
            </div>
          </section>
        )}

        {data && activeUser && (
          <section className="settings personalization">
            <h3>Personalization</h3>
            {preferencesSummary ? (
              <>
                <span className="muted small">
                  {preferencesSummary.active
                    ? `Learning from ${formatNumber(preferencesSummary.events)} explicit edits`
                    : preferencesSummary.events > 0
                      ? `${formatNumber(preferencesSummary.events)} edits recorded · ${formatNumber(preferencesSummary.needed)} more to personalize`
                      : "No edits yet — select, reject, or rate photos to teach this profile."}
                </span>
                {preferencesSummary.labels.length > 0 && (
                  <div className="preference-labels">
                    {preferencesSummary.labels.slice(0, 5).map((item) => (
                      <span
                        key={item.label}
                        className={`preference-label ${item.rate >= 0.5 ? "positive" : "negative"}`}
                        title={`Keep rate ${(item.rate * 100).toFixed(0)}%`}
                      >
                        {item.rate >= 0.5 ? "+" : "−"} {item.label.replace(/_/g, " ")}
                      </span>
                    ))}
                  </div>
                )}
                <div className="setting-actions">
                  <button
                    type="button"
                    className="ghost small"
                    onClick={() => void resetPreferences()}
                    disabled={busy || preferencesSummary.events === 0}
                  >
                    Reset learning
                  </button>
                </div>
                <span className="fineprint">Only explicit edits from this profile are used, locally.</span>
              </>
            ) : (
              <span className="muted small">Loading preferences…</span>
            )}
          </section>
        )}

        <footer className="sidebar-foot">
          <span className="privacy-dot" aria-hidden="true" />
          <span>Local only. No uploads.</span>
          <span className="muted">{appInfo ? `v${appInfo.version} · Python ${appInfo.python}` : ""}</span>
        </footer>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="topbar-title">
            <span className="eyebrow">Review workspace · {activeUser?.name ?? "Profile"}</span>
            <h1>{data?.project.name ?? "Topshot"}</h1>
            <span className="muted path" title={data?.project.sourceDir ?? ""}>
              {data?.project.sourceDir ? shortPath(data.project.sourceDir, 2) : "No shoot imported"}
            </span>
          </div>

          <ol className="steps" aria-label="Workflow">
            <li className={stepState(0, currentStep, false)}>
              <span>1</span> Import
            </li>
            <li className={stepState(1, currentStep, !!data)}>
              <span>2</span> Analyze
            </li>
            <li className={stepState(2, currentStep, data?.project.status === "ready")}>
              <span>3</span> Review
            </li>
            <li className={stepState(3, currentStep, !!copySummary)}>
              <span>4</span> Copy
            </li>
          </ol>

          <div className="selection-pill" aria-live="polite">
            {data ? (
              <>
                <strong>{formatNumber(data.counts.selectedImages)} selected</strong>
                <span>· {formatBytes(data.counts.selectedBytes)}</span>
                {data.project.keepTarget ? <span>· target {formatNumber(data.project.keepTarget)}</span> : null}
              </>
            ) : (
              <span>No selection</span>
            )}
          </div>

          <div className="topbar-shortcuts" aria-label="Keyboard shortcuts">
            <span className="shortcut-heading">Shortcuts</span>
            <span>
              <kbd>1</kbd>–<kbd>5</kbd> rate
            </span>
            <span>
              <kbd>S</kbd> select
            </span>
            <span>
              <kbd>X</kbd> exclude
            </span>
            <span>
              <kbd>U</kbd> unrate
            </span>
            <span>
              <kbd>Space</kbd> preview
            </span>
            <span>
              <kbd>B</kbd> burst
            </span>
            <span>
              <kbd>⌘Z</kbd> undo
            </span>
            <span className="shortcut-hover">Hover a photo to target it</span>
          </div>
        </header>

        {job && (pipelineBusy || job.status === "failed") ? (
          <ProgressView
            job={job}
            projectName={data?.project.name ?? "Shoot"}
            onCancel={() => void cancelJob(job.id)}
            onDismiss={job.status === "failed" ? () => setJob(null) : undefined}
          />
        ) : !data ? (
          <Welcome dragging={dragOver} busy={creatingSource} onChoose={chooseSource} />
        ) : (
          <>
            {data.project.status === "interrupted" && (
              <div className="banner">
                <span>Analysis was interrupted. Completed photos are ready to review.</span>
                <button type="button" className="ghost small" onClick={() => void resumeAnalysis()} disabled={busy}>
                  Resume analysis
                </button>
              </div>
            )}
            {data.project.status === "error" && (
              <div className="banner error">
                <span>The last analysis failed. You can retry the scan.</span>
                <button type="button" className="ghost small" onClick={() => void resumeAnalysis()} disabled={busy}>
                  Retry
                </button>
              </div>
            )}

            <div className="review-toolbar">
              <div className="chips" role="group" aria-label="Filters">
                {(
                  [
                    ["all", "All", data.counts.all],
                    ["keep", "Keeps", data.counts.keep],
                    ["review", "Review", data.counts.review],
                    ["reject", "Rejects", data.counts.reject],
                    ["selected", "Selected", data.counts.selected],
                    ["people", "People", data.counts.people],
                    ["duplicates", "Similar", data.counts.duplicates],
                    ["docs", "Docs", data.counts.documentation],
                  ] as Array<[FilterKey, string, number]>
                ).map(([key, label, count]) => (
                  <button
                    type="button"
                    key={key}
                    className={`chip ${filter === key ? "active" : ""}`}
                    aria-pressed={filter === key}
                    onClick={() => setFilter(key)}
                  >
                    {label}
                    <span>{formatNumber(count)}</span>
                  </button>
                ))}
              </div>

              <div className="toolbar-meta">
                <label className="target-control" title="Select the best N photos, one per burst or similar group. People photos use eyes/smiles; landscapes use visual quality. Applying a target re-culls; edits afterwards are kept until the target changes.">
                  <span>Keep top</span>
                  <input
                    type="number"
                    min={1}
                    max={data.project.photoCount}
                    placeholder="e.g. 500"
                    value={targetInput}
                    onChange={(event) => setTargetInput(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        commitKeepTarget();
                      }
                    }}
                    disabled={busy}
                    aria-label="Number of photos to keep"
                  />
                  <button type="button" className="ghost small" onClick={commitKeepTarget} disabled={busy}>
                    Apply
                  </button>
                  {data.project.keepTarget ? (
                    <button type="button" className="ghost small" onClick={clearKeepTarget} disabled={busy}>
                      All
                    </button>
                  ) : null}
                </label>
                <button
                  type="button"
                  className="primary"
                  onClick={() => void openCopyDialog()}
                  disabled={busy || data.counts.selectedImages === 0}
                >
                  Copy Keeps · {formatNumber(data.counts.selectedImages)}
                </button>
              </div>
            </div>

            {(scan?.videos || scan?.unsupported) && (
              <div className="scan-note">
                <span>
                  Imported {formatNumber(scan.supported)} photos
                  {scan.videos ? ` · ${formatNumber(scan.videos)} videos skipped` : ""}
                  {scan.unsupported ? ` · ${formatNumber(scan.unsupported)} unsupported files skipped` : ""}
                  {scan.sidecars ? ` · ${formatNumber(scan.sidecars)} XMP sidecars` : ""}
                  {data.counts.people ? ` · ${formatNumber(data.counts.people)} with people` : ""}
                </span>
                {scan.errors?.length ? <span className="warn-text">{scan.errors.length} scan errors</span> : null}
              </div>
            )}

            {loading && !data ? (
              <div className="loading">Loading shoot…</div>
            ) : (
              <div className="grid-scroll" key={`${data.project.id}-${filter}`}>
                <ReviewGrid
                  photos={filteredPhotos}
                  bursts={data.bursts}
                  groupBursts={filter === "all"}
                  expanded={expanded}
                  expandedDuplicates={expandedDuplicates}
                  focusedId={focusedId}
                  resetKey={`${data.project.id}-${filter}`}
                  onFocus={setFocusedId}
                  onHover={setHoveredId}
                  onToggle={toggleSelect}
                  onOpen={openLoupe}
                  onToggleBurst={toggleBurst}
                  onToggleDuplicate={toggleDuplicate}
                  onSwapPhoto={(photo, kind, groupId) => void swapGroupPhoto(photo, kind, groupId)}
                />
              </div>
            )}

            <footer className="shortcut-bar">
              <span>
                <kbd>1</kbd>–<kbd>5</kbd> rate
              </span>
              <span>
                <kbd>X</kbd> exclude
              </span>
              <span>
                <kbd>S</kbd> select
              </span>
              <span>
                <kbd>U</kbd> unrate
              </span>
              <span>
                <kbd>Space</kbd> preview
              </span>
              <span>
                <kbd>B</kbd> burst
              </span>
              <span>
                <kbd>⌘Z</kbd> undo
              </span>
              <span className="muted">Rejects only exclude from the copy.</span>
            </footer>
          </>
        )}
      </main>

      {loupePhoto && (
        <Loupe
          photo={loupePhoto}
          index={Math.max(0, loupeGroupIndex)}
          total={Math.max(1, loupeGroupSequence.length)}
          similarGroupsRemaining={similarGroupsRemaining}
          similarGroupsTotal={loupeSimilarGroupIds.length}
          onClose={() => setLoupeId(null)}
          onPrev={() => navigateGroup(-1)}
          onNext={() => navigateGroup(1)}
          onToggleSelect={toggleSelect}
          onReject={rejectPhoto}
          onRate={ratePhoto}
          onUnrate={unratePhoto}
          groupMembers={loupeGroup.members}
          groupKind={loupeGroup.kind}
          groupId={loupeGroup.id}
          onJumpToPhoto={(id) => {
            setLoupeId(id);
            setFocusedId(id);
          }}
          onSwapGroup={(photo, kind, groupId) => void swapGroupPhoto(photo, kind, groupId)}
          onUndo={() => void undo()}
          canUndo={undoStack.length > 0}
          zoom={loupeZoom}
          onToggleZoom={nextLoupeZoom}
          showStrip={loupeShowStrip}
          onToggleStrip={() => setLoupeShowStrip((value) => !value)}
        />
      )}

      {preflight && (
        <CopyDialog
          preflight={preflight}
          profileName={activeUser?.name ?? "Profile"}
          busy={copyStarting}
          onCancel={() => setPreflight(null)}
          onConfirm={() => void confirmCopy()}
          onChangeDestination={async () => {
            const folder = await chooseDestination();
            if (folder) {
              try {
                if (!data) return;
                const result = await rpc<Preflight>("copy.preflight", {
                  projectId: data.project.id,
                  userId: activeUserId ?? 1,
                  fileIds: data.photos.filter((photo) => photo.selected).map((photo) => photo.id),
                  destination: folder,
                });
                setPreflight(result);
              } catch (error) {
                setToast(readError(error));
              }
            }
          }}
        />
      )}

      {copySummary && (
        <CopySummaryModal
          result={copySummary}
          onClose={() => setCopySummary(null)}
          onOpenDestination={() => void handleOpenDestination()}
        />
      )}

      {copyBusy && copyJob && (
        <div className="modal-backdrop">
          <ProgressView
            job={copyJob}
            projectName={data?.project.name ?? "Shoot"}
            onCancel={() => void cancelJob(copyJob.id)}
          />
        </div>
      )}

      {copyJob?.status === "failed" && (
        <div className="modal-backdrop">
          <ProgressView
            job={copyJob}
            projectName={data?.project.name ?? "Shoot"}
            onCancel={() => setCopyJob(null)}
            onDismiss={() => setCopyJob(null)}
          />
        </div>
      )}

      {dragOver && (
        <div className="drop-overlay">
          <div>
            <strong>Drop the shoot folder</strong>
            <span>Import is recursive and read-only.</span>
          </div>
        </div>
      )}

      {toast && (
        <button type="button" className="toast" onClick={() => setToast(null)}>
          {toast}
        </button>
      )}
    </div>
  );
}
