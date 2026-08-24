"""HTTP and WebSocket surface for audio-ingestion.

The response envelope and error handlers live in the shared ``api_envelope``
package, not here — every service returns the same two shapes and one definition
of them is the point. The envelope covers the HTTP half only; a WebSocket
carries frames, not envelopes.
"""
