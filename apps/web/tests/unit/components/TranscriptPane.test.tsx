import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { TranscriptPane } from '../../../src/components/TranscriptPane';
import type { TranscriptStreamState } from '../../../src/hooks/useTranscriptStream';
import type { TranscriptSegment } from '../../../src/transcript/segment';

/**
 * The one requirement this component exists for: an empty pane must never read
 * as "nobody is speaking."
 *
 * Redis pub/sub keeps no history, so a connection opened late or reopened after
 * a drop starts from silence. "Connected, nothing said yet" and "not connected"
 * therefore produce the same empty list, and the tests below assert they produce
 * different *screens* — which is the whole of TASK-070's transcript bullet.
 */

const SEGMENT: TranscriptSegment = {
  sessionId: '11111111-1111-4111-8111-111111111111',
  resultId: 'result-1',
  text: 'Patient reports right knee pain for about six weeks.',
  startTime: 12.34,
  endTime: 16.78,
};

function renderPane(state: TranscriptStreamState, segments: TranscriptSegment[] = []) {
  const onRetry = vi.fn();
  render(<TranscriptPane state={state} segments={segments} onRetry={onRetry} />);
  return { onRetry };
}

describe('an empty pane says which kind of empty it is', () => {
  it('distinguishes connected-and-quiet from not-connected', () => {
    renderPane({ status: 'open' });
    const connected = screen.getByTestId('transcript-status').textContent;
    screen.getByTestId('transcript-status').remove();

    renderPane({
      status: 'error',
      error: { code: 'STREAM_FAILED', message: 'The transcript connection dropped.' },
    });
    const disconnected = screen.getByTestId('transcript-status').textContent;

    // The assertion that matters is that these are not the same screen. Both
    // render zero segments, and a pane that showed one blank box for both would
    // tell a provider the room is quiet when nothing is being received.
    expect(connected).not.toEqual(disconnected);
  });

  it('says nothing has been said yet when connected and quiet', () => {
    renderPane({ status: 'open' });

    expect(screen.getByTestId('transcript-status')).toHaveTextContent('Nothing has been said yet');
  });

  it('says it is still connecting before the socket opens', () => {
    renderPane({ status: 'connecting' });

    expect(screen.getByTestId('transcript-status')).toHaveTextContent('Connecting');
  });

  it('shows the stream error when the connection is down', () => {
    renderPane({
      status: 'error',
      error: { code: 'STREAM_FAILED', message: 'The transcript connection dropped.' },
    });

    expect(screen.getByTestId('transcript-status')).toHaveTextContent('connection dropped');
  });
});

describe('the status line is not conditional on there being speech', () => {
  /**
   * The status line is the thing that stops an empty pane being ambiguous, so it
   * must not be the thing that disappears when the pane is empty — nor when it
   * is full, since a dropped connection mid-encounter leaves segments on screen
   * and it is exactly then that a provider needs to be told the feed has stopped.
   */
  it('is still shown when the connection drops with speech already on screen', () => {
    renderPane(
      {
        status: 'error',
        error: { code: 'STREAM_FAILED', message: 'The transcript connection dropped.' },
      },
      [SEGMENT],
    );

    expect(screen.getByTestId('transcript-status')).toHaveTextContent('connection dropped');
    expect(screen.getByText(SEGMENT.text)).toBeInTheDocument();
  });
});

describe('reconnecting', () => {
  it('offers a reconnect only while the stream is down', () => {
    renderPane({ status: 'open' });
    expect(screen.queryByTestId('retry-transcript')).not.toBeInTheDocument();

    renderPane({
      status: 'error',
      error: { code: 'STREAM_FAILED', message: 'dropped' },
    });
    expect(screen.getByTestId('retry-transcript')).toBeInTheDocument();
  });
});

describe('rendering speech', () => {
  it('lists segments in the order they arrived', () => {
    renderPane({ status: 'open' }, [
      SEGMENT,
      { ...SEGMENT, resultId: 'result-2', text: 'It hurts going downstairs.' },
    ]);

    const items = screen.getByTestId('transcript-segments').querySelectorAll('li');
    expect([...items].map((item) => item.textContent)).toEqual([
      SEGMENT.text,
      'It hurts going downstairs.',
    ]);
  });

  /**
   * Announced politely rather than assertively. Speech arriving is not an alert,
   * and interrupting a screen reader for every utterance would make the nudges —
   * which are assertive, and are the thing worth interrupting for —
   * indistinguishable from ordinary conversation.
   */
  it('announces new speech without interrupting', () => {
    renderPane({ status: 'open' }, [SEGMENT]);

    expect(screen.getByTestId('transcript-segments')).toHaveAttribute('aria-live', 'polite');
  });
});
