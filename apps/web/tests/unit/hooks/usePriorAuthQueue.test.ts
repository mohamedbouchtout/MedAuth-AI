/**
 * The queue hook's paging and refresh behaviour (TASK-072).
 *
 * The screen tests cover the happy path. What is here is the behaviour a
 * rendered test would struggle to pin down: what happens to rows already on
 * screen when a *further* page fails, and that a filter change does not leave
 * the previous filter's rows showing as though they had matched.
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { PriorAuthApi } from '../../../src/api/priorAuth';
import { usePriorAuthQueue } from '../../../src/hooks/usePriorAuthQueue';
import { aRequest, priorAuthServing, PROVIDER_ID } from '../../support/priorAuth';

describe('paging', () => {
  it('keeps the rows already read when a further page fails', async () => {
    // A failed second page is not a reason to discard what the provider is
    // working through, and the cursor is kept so pressing again retries it.
    const failure = {
      ok: false as const,
      failure: { kind: 'network' as const, message: 'unreachable' },
    };
    const api: PriorAuthApi = {
      ...priorAuthServing(),
      listRequests: vi
        .fn<PriorAuthApi['listRequests']>()
        .mockResolvedValueOnce({
          ok: true,
          value: { requests: [aRequest({ requestId: 'first' })], nextCursor: 'cursor-1' },
        })
        .mockResolvedValueOnce(failure),
    };

    const { result } = renderHook(() => usePriorAuthQueue(PROVIDER_ID, { api }));
    await waitFor(() => expect(result.current.state.kind).toBe('loaded'));

    act(() => result.current.loadMore());
    await waitFor(() => {
      const state = result.current.state;
      expect(state.kind === 'loaded' && state.loadingMore).toBe(false);
    });

    const state = result.current.state;
    expect(state.kind === 'loaded' && state.requests).toHaveLength(1);
    expect(state.kind === 'loaded' && state.nextCursor).toBe('cursor-1');
  });

  it('does nothing when there is no next page', async () => {
    const api = priorAuthServing();

    const { result } = renderHook(() => usePriorAuthQueue(PROVIDER_ID, { api }));
    await waitFor(() => expect(result.current.state.kind).toBe('loaded'));

    act(() => result.current.loadMore());

    expect(api.listRequests).toHaveBeenCalledTimes(1);
  });
});

describe('refresh', () => {
  it('reads the first page again', async () => {
    const api = priorAuthServing();

    const { result } = renderHook(() => usePriorAuthQueue(PROVIDER_ID, { api }));
    await waitFor(() => expect(result.current.state.kind).toBe('loaded'));

    act(() => result.current.refresh());
    await waitFor(() => expect(api.listRequests).toHaveBeenCalledTimes(2));

    expect(api.listRequests).toHaveBeenLastCalledWith(PROVIDER_ID, {});
  });
});

describe('replace', () => {
  it('swaps one row without refetching', async () => {
    const api = priorAuthServing({
      pages: [{ requests: [aRequest({ requestId: 'r1' })], nextCursor: null }],
    });

    const { result } = renderHook(() => usePriorAuthQueue(PROVIDER_ID, { api }));
    await waitFor(() => expect(result.current.state.kind).toBe('loaded'));

    act(() =>
      result.current.replace(
        aRequest({ requestId: 'r1', status: 'submitted', rawStatus: 'submitted' }),
      ),
    );

    const state = result.current.state;
    expect(state.kind === 'loaded' && state.requests[0]?.status).toBe('submitted');
    expect(api.listRequests).toHaveBeenCalledTimes(1);
  });

  it('is a no-op while the queue is still loading', async () => {
    const api = priorAuthServing();

    const { result } = renderHook(() => usePriorAuthQueue(PROVIDER_ID, { api }));

    act(() => result.current.replace(aRequest()));

    expect(result.current.state.kind).toBe('loading');
    await waitFor(() => expect(result.current.state.kind).toBe('loaded'));
  });
});

describe('changing the query', () => {
  it('does not show the previous filter’s rows while the new one loads', async () => {
    // Showing them would read as the new filter having matched them.
    const api = priorAuthServing();

    const { result, rerender } = renderHook(
      ({ status }: { status?: string }) =>
        usePriorAuthQueue(PROVIDER_ID, status === undefined ? { api } : { api, status }),
      { initialProps: {} as { status?: string } },
    );
    await waitFor(() => expect(result.current.state.kind).toBe('loaded'));

    rerender({ status: 'denied' });

    expect(result.current.state.kind).toBe('loading');
    await waitFor(() => expect(result.current.state.kind).toBe('loaded'));
    expect(api.listRequests).toHaveBeenLastCalledWith(PROVIDER_ID, { status: 'denied' });
  });
});
