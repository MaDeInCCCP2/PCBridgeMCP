"""Owned Chromium process with an animated WebGL surface for DXGI validation."""
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--window-size=900,600", "--window-position=120,120"])
    page = browser.new_page(viewport=None, no_viewport=True)
    page.set_content("<title>PCBridge DXGI Validation</title><canvas width=500 height=260></canvas>")
    renderer = page.evaluate("""() => {
        const gl = document.querySelector("canvas").getContext("webgl", {alpha:false});
        if (!gl) return "";
        const extension = gl.getExtension("WEBGL_debug_renderer_info");
        function draw(t) {
            gl.clearColor(0.75, (Math.sin(t/200)+1)/2, 0.18, 1);
            gl.clear(gl.COLOR_BUFFER_BIT);
            requestAnimationFrame(draw);
        }
        requestAnimationFrame(draw);
        return extension ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) : "WebGL renderer unavailable";
    }""")
    Path(sys.argv[1]).write_text(renderer, encoding="utf-8")
    page.bring_to_front()
    page.wait_for_timeout(20000)
    browser.close()
