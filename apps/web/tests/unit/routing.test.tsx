/**
 * The routes this app has, and why each is where it is (TASK-071, TASK-072).
 *
 * TASK-070 deferred installing a router and named the condition that would end
 * the deferral: "a requirement that the note review screen be linkable or
 * survive a refresh". These tests are that requirement, stated as behaviour.
 *
 * The property most worth protecting is that **the note route sits outside the
 * launch gate**. The note routes take no credential in v1, so a linked or
 * reloaded review screen is a legitimate way to arrive at a note — and putting
 * it behind the gate would show a provider the sign-in screen for a note they
 * can read perfectly well. What they lose by arriving that way is the launch,
 * which is a credential and is never persisted; the chart write says so rather
 * than failing when it is pressed.
 *
 * TASK-072's queue follows the same placement for a different reason. It is
 * outside the gate too, but what it does without a launch is say that signing in
 * through the EHR is what fills it — because unlike a note, a queue *does* need
 * an identity, and an empty list would be a claim about the provider's work
 * rather than about this app's sign-in state.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { aNote, SESSION_ID } from '../support/notes';

const readNote = vi.fn(() => Promise.resolve({ ok: true as const, value: aNote() }));

vi.mock('../../src/api/notes', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/notes')>();
  return { ...actual, notesApi: { readNote, updateNote: vi.fn() } };
});

const { App } = await import('../../src/App');
const { NoteReviewRoute } = await import('../../src/screens/NoteReviewRoute');

function locationAt(pathname: string): Location {
  return { pathname, search: '' } as Location;
}

/** The route element under its own path, which is where `useParams` reads from. */
function renderRoute(element: React.ReactNode, at = `/notes/${SESSION_ID}`) {
  render(
    <MemoryRouter initialEntries={[at]}>
      <Routes>
        <Route path="/notes/:sessionId" element={element} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderAt(path: string) {
  const history = { replaceState: vi.fn() } as unknown as History;
  render(
    <MemoryRouter initialEntries={[path]}>
      <App location={locationAt(path)} history={history} />
    </MemoryRouter>,
  );
}

describe('the note route', () => {
  /**
   * A provider interrupted during chart review comes back to this URL. Before
   * the router the screen was an in-memory phase of one visit and a reload lost
   * it entirely.
   */
  it('renders a note for a link, with no launch in hand', async () => {
    renderAt(`/notes/${SESSION_ID}`);

    await waitFor(() => expect(screen.getByTestId('section-soapPlan')).toBeInTheDocument());
    expect(readNote).toHaveBeenCalledWith(SESSION_ID);
  });

  it('does not show the sign-in screen for a note it can legitimately read', async () => {
    renderAt(`/notes/${SESSION_ID}`);

    await waitFor(() => expect(screen.getByTestId('section-soapPlan')).toBeInTheDocument());
    expect(screen.queryByTestId('no-issuer')).not.toBeInTheDocument();
  });

  /** A launch is a credential, so a page reached without one reports that. */
  it('reports that it holds no launch rather than offering a write that would fail', async () => {
    renderAt(`/notes/${SESSION_ID}`);

    await waitFor(() => expect(screen.getByTestId('write-no-launch')).toBeInTheDocument());
    expect(screen.queryByTestId('write-to-ehr')).not.toBeInTheDocument();
  });

  it('still gates the visit flow behind a launch', () => {
    renderAt('/');

    expect(screen.getByTestId('no-issuer')).toBeInTheDocument();
  });
});

describe('which note the route reads', () => {
  /**
   * The URL decides, never the page's memory. A completed visit held from
   * earlier must not leak its launch or its chart entry onto a different
   * session's note — that would offer a chart write against the wrong encounter.
   */
  it('ignores a held visit that names a different session', async () => {
    const other = {
      session: { sessionId: 'a-different-session', jwt: 'j' },
      launchId: 'launch-7',
      ehrEncounterId: 'Encounter/7',
    };
    renderRoute(<NoteReviewRoute completed={other} launchId={null} />);

    await waitFor(() => expect(screen.getByTestId('write-no-launch')).toBeInTheDocument());
    expect(screen.queryByTestId('write-to-ehr')).not.toBeInTheDocument();
  });

  it('uses the held visit when it names the session in the URL', async () => {
    const same = {
      session: { sessionId: SESSION_ID, jwt: 'j' },
      launchId: 'launch-7',
      ehrEncounterId: 'Encounter/7',
    };
    renderRoute(<NoteReviewRoute completed={same} launchId="launch-7" />);

    await waitFor(() => expect(screen.getByTestId('write-to-ehr')).toBeInTheDocument());
  });
});

describe('the prior-auth route', () => {
  /**
   * The destination TASK-070 predicted when it deferred the router: a screen a
   * provider arrives at rather than walks to through a visit.
   */
  it('renders the queue rather than the visit flow', async () => {
    renderAt('/prior-auth');

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Prior authorizations' })).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('no-issuer')).not.toBeInTheDocument();
  });

  it('says what it needs instead of showing an empty queue with no launch', async () => {
    // An empty list here would tell a provider they have no outstanding
    // authorizations, which is a different claim from "you are not signed in"
    // and might be false.
    renderAt('/prior-auth');

    await waitFor(() => expect(screen.getByTestId('queue-unavailable')).toBeInTheDocument());
    expect(screen.queryByTestId('queue-empty')).not.toBeInTheDocument();
  });
});
