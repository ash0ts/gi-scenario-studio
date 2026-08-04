import { createContext, useCallback, useContext, useReducer, useRef, type Dispatch, type ReactNode, type SetStateAction } from "react";

interface DraftEntry {
  initial: unknown;
  value: unknown;
  dirty: boolean;
}

interface DraftStore {
  entries: Map<string, DraftEntry>;
  refresh: () => void;
}

const DraftContext = createContext<DraftStore | null>(null);

/**
 * Keeps unconfirmed editor drafts alive while a project workspace is mounted.
 * These values are view state only: worker saves remain the sole path into the
 * authoritative ecological record.
 */
export function WorkspaceDraftProvider({ children }: { children: ReactNode }) {
  const entries = useRef(new Map<string, DraftEntry>());
  const [, rerender] = useReducer((value: number) => value + 1, 0);
  const refresh = useCallback(() => rerender(), []);
  return <DraftContext.Provider value={{ entries: entries.current, refresh }}>{children}</DraftContext.Provider>;
}

export function useWorkspaceDraft<T>(key: string, createInitial: () => T): [T, Dispatch<SetStateAction<T>>, () => void, boolean, () => void, (expected: T, committed?: T) => boolean] {
  const store = useContext(DraftContext);
  if (!store) throw new Error("useWorkspaceDraft must be used inside WorkspaceDraftProvider");
  let entry = store.entries.get(key);
  if (!entry) {
    const initial = createInitial();
    entry = { initial, value: initial, dirty: false };
    store.entries.set(key, entry);
  }
  const setValue: Dispatch<SetStateAction<T>> = (next) => {
    const current = store.entries.get(key) as DraftEntry;
    current.value = typeof next === "function" ? (next as (value: T) => T)(current.value as T) : next;
    current.dirty = true;
    store.refresh();
  };
  const undo = () => {
    const current = store.entries.get(key);
    if (!current) return;
    current.value = current.initial;
    current.dirty = false;
    store.refresh();
  };
  const commit = () => {
    const current = store.entries.get(key);
    if (!current) return;
    current.initial = current.value;
    current.dirty = false;
    store.refresh();
  };
  const commitIfUnchanged = (expected: T, committed: T = expected) => {
    const current = store.entries.get(key);
    if (!current) return false;
    const currentJson = JSON.stringify(current.value);
    const committedJson = JSON.stringify(committed);
    if (currentJson !== JSON.stringify(expected)) {
      current.initial = committed;
      current.dirty = currentJson !== committedJson;
      if (!current.dirty) current.value = committed;
      store.refresh();
      return !current.dirty;
    }
    current.initial = committed;
    current.value = committed;
    current.dirty = false;
    store.refresh();
    return true;
  };
  return [entry.value as T, setValue, undo, entry.dirty, commit, commitIfUnchanged];
}
