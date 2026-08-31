# Third-party notices

## Qwen3-TTS

OpenMontage's optional embedded local TTS runtime uses the official `qwen-tts` Python package and Qwen3-TTS model family from [QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS). The upstream source repository is licensed under Apache License 2.0. Model files are downloaded or imported into an ignored local cache and are not redistributed by this repository. Users should review the current model card and terms before redistribution or commercial deployment.

## Voicebox migration compatibility

The optional migration script reads a user's existing local Voicebox profile database and copies user-owned reference audio. OpenMontage does not vendor, launch, or redistribute the Voicebox desktop application or server. Voicebox is available separately from [jamiepine/voicebox](https://github.com/jamiepine/voicebox) under the MIT License.

## GSAP

The HyperFrames renderer vendors the minified GSAP browser runtime, version 3.14.2, from [GreenSock/GSAP](https://github.com/greensock/GSAP). The file is kept at `tools/video/vendor/gsap/gsap.min.js` so local validation and rendering do not depend on a CDN. Upstream notices are preserved beside the vendored file; review the upstream license before redistribution outside this repository.
