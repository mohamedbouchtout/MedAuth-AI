/**
 * Loading a provider's prior-authorization queue, a page at a time (TASK-072).
 *
 * **No polling, unlike `useNote`.** That hook waits because it is reached by
 * ending a visit and the note it wants is being generated as the screen opens —
 * a wait with a known end. Nothing here is mid-flight: a payer's decision
 * arrives on the payer's own schedule, often days later, so re-asking on a timer
 * would be a request per interval per open tab forever. The provider refreshes.
 *
 * **Pages accumulate rather than replace.** "More" appends, because a queue a
 * provider is working through should not lose what they have already read.
 *
 * Nothing here logs: the queue names a provider, and the rows name visits.
 */

import type { ApiFailure } from '@medauth/session-client';
import { useCallback, useEffect, useState } from 'react';

import { priorAuthApi, type PriorAuthApi, type PriorAuthSummary } from '../api/priorAuth';

/** Where the load is. */
export type QueueLoad =
  | { kind: 'loading' }
  | { kind: 'error'; failure: ApiFailure }
  | {
      kind: 'loaded';
      requests: PriorAuthSummary[];
      /** Null when every page has been read. */
      nextCursor: string | null;
      /** True while a further page is in flight. */
      loadingMore: boolean;
    };

export interface UsePriorAuthQueue {
  state: QueueLoad;
  /** Read the next page and append it. A no-op when there is none. */
  loadMore: () => void;
  /** Start again from the first page. */
  refresh: () => void;
  /** Put an updated row in place after a resubmission, without refetching. */
  replace: (request: PriorAuthSummary) => void;
}

export function usePriorAuthQueue(
  providerId: string,
  options: { status?: string; api?: PriorAuthApi } = {},
): UsePriorAuthQueue {
  const { status, api = priorAuthApi } = options;
  const [state, setState] = useState<QueueLoad>({ kind: 'loading' });
  const [reloads, setReloads] = useState(0);

  /**
   * Which query the held state describes.
   *
   * Reset during render rather than in the effect, which is React's own
   * recommendation for adjusting state when a prop changes — and here it is also
   * what stops a filter change from showing the previous filter's rows for a
   * frame, which would read as the filter having matched them.
   */
  const [loadedFor, setLoadedFor] = useState(`${providerId}|${status ?? ''}`);
  const key = `${providerId}|${status ?? ''}`;
  if (key !== loadedFor) {
    setLoadedFor(key);
    setState({ kind: 'loading' });
  }

  useEffect(() => {
    let cancelled = false;

    void (async () => {
      const result = await api.listRequests(
        providerId,
        status === undefined ? {} : { status },
      );
      if (cancelled) {
        return;
      }
      setState(
        result.ok
          ? {
              kind: 'loaded',
              requests: result.value.requests,
              nextCursor: result.value.nextCursor,
              loadingMore: false,
            }
          : { kind: 'error', failure: result.failure },
      );
    })();

    return () => {
      cancelled = true;
    };
  }, [api, providerId, status, reloads]);

  const loadMore = useCallback(() => {
    setState((current) => {
      if (current.kind !== 'loaded' || current.nextCursor === null || current.loadingMore) {
        return current;
      }
      const cursor = current.nextCursor;

      void (async () => {
        const result = await api.listRequests(providerId, {
          cursor,
          ...(status === undefined ? {} : { status }),
        });
        setState((latest) => {
          if (latest.kind !== 'loaded') {
            return latest;
          }
          if (!result.ok) {
            // The rows already read stay on screen: a failed *further* page is
            // not a reason to discard what the provider is working through. The
            // cursor is kept too, so pressing again retries the same page.
            return { ...latest, loadingMore: false };
          }
          return {
            kind: 'loaded',
            requests: [...latest.requests, ...result.value.requests],
            nextCursor: result.value.nextCursor,
            loadingMore: false,
          };
        });
      })();

      return { ...current, loadingMore: true };
    });
  }, [api, providerId, status]);

  const refresh = useCallback(() => {
    setState({ kind: 'loading' });
    setReloads((count) => count + 1);
  }, []);

  const replace = useCallback((request: PriorAuthSummary) => {
    setState((current) => {
      if (current.kind !== 'loaded') {
        return current;
      }
      return {
        ...current,
        requests: current.requests.map((row) =>
          row.requestId === request.requestId ? request : row,
        ),
      };
    });
  }, []);

  return { state, loadMore, refresh, replace };
}
