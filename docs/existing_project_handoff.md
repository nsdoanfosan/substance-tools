# Existing Painter project handoff

After issuing one native UPDATE, call
`get_painter_transfer_api(1)['queue_existing_painter_project']()` to queue that
exact request for its saved SPP. The function verifies the current Blender
project path and refuses to overwrite another pending handoff.

Painter waits while busy. It leaves unsaved changes alone by default. A caller
with authorization to save the current project may pass
`preserve_open_project=True`; only an already named, existing project is saved.
The next idle poll closes the saved project and opens the requested existing
SPP. The normal request-matching, claim, bake, save and ticket-removal flow then
handles UPDATE. No new CREATE or second bake request is generated.

The project operations use the native [Painter project API](https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/project).
