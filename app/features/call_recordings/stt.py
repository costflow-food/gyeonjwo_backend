"""OpenAI 음성 인식(STT) 어댑터. 파일을 스트리밍으로 보내고 결과를 발화 목록(Segment)으로 맞춘다.

- 화자 분리 모델(gpt-4o-transcribe-diarize): response_format=diarized_json, 발화마다 화자·시각
- 그 밖의 모델(gpt-transcribe 등): 문맥 설명·예상 단어를 넘기고, 화자 없는 발화 하나
"""
import time
from pathlib import Path

import httpx

from app.features.call_recordings.transcription import Segment, TranscriptionError

DIARIZE_MODELS = frozenset({'gpt-4o-transcribe-diarize'})


def stt_hints(item_name: str | None, supplier_name: str | None) -> tuple[str, list[str]]:
    """일반 모델에 넘기는 문맥 설명과 예상 단어. 금액·수량을 숫자로 적게 한다."""
    target = f'공급업체 {supplier_name}에' if supplier_name else '공급업체에'
    item = f' {item_name}' if item_name else ''
    prompt = f'식자재 구매 담당자가 {target}{item} 견적을 묻는 한국어 전화 통화. 금액과 수량은 숫자로 적는다(예: 4,100원, 800kg).'
    words = [word for word in dict.fromkeys([item_name, supplier_name, 'kg', '박스']) if word]
    return prompt, words


class OpenAiTranscriber:
    def __init__(self, settings, transport=None):
        """settings의 LLM 주소·키·STT 모델을 쓴다. transport는 테스트에서 HTTP를 바꿔 끼울 때만 준다."""
        self.settings, self.transport = settings, transport

    @property
    def model(self) -> str:
        """설정의 STT 모델."""
        return self.settings.llm_stt_model

    def transcribe(self, path: Path, file_name: str, *, item_name=None, supplier_name=None) -> tuple[list[Segment], float | None, dict]:
        """파일 하나를 전사한다. 반환: (발화 목록, 길이 초, 사용량). 실패는 TranscriptionError(code, 재시도 여부)."""
        self.settings.require_llm()
        model = self.model
        if model in DIARIZE_MODELS:
            data = {'model': model, 'response_format': 'diarized_json', 'chunking_strategy': 'auto', 'language': 'ko'}
        else:
            prompt, words = stt_hints(item_name, supplier_name)
            data = {'model': model, 'prompt': prompt, 'languages[]': ['ko'], 'keywords[]': words}
        started = time.monotonic()
        try:
            with open(path, 'rb') as handle, httpx.Client(timeout=self.settings.stt_timeout_seconds,
                                                          transport=self.transport) as client:
                response = client.post(str(self.settings.llm_base_url).rstrip('/') + '/audio/transcriptions',
                                       headers={'Authorization': 'Bearer ' + self.settings.llm_api_key.get_secret_value()},
                                       data=data, files={'file': (Path(file_name).name or 'recording', handle)})
        except httpx.TimeoutException:
            raise TranscriptionError('STT_TIMEOUT', True) from None
        except httpx.HTTPError:
            raise TranscriptionError('STT_UNAVAILABLE', True) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise TranscriptionError('STT_UNAVAILABLE', True)
        if response.status_code != 200:
            raise TranscriptionError('STT_REJECTED', False)
        try:
            payload = response.json()
        except ValueError:
            raise TranscriptionError('STT_INVALID_RESPONSE', True) from None
        segments = self.segments(payload)
        if not any(segment.text.strip() for segment in segments):
            raise TranscriptionError('STT_NO_SPEECH', False)
        usage = dict(payload.get('usage') or {}) | {'model': model, 'seconds': round(time.monotonic() - started, 1)}
        duration = payload.get('duration') or (payload.get('usage') or {}).get('seconds')
        return segments, (float(duration) if isinstance(duration, (int, float)) else None), usage

    @staticmethod
    def segments(payload: dict) -> list[Segment]:
        """응답을 발화 목록으로. 화자 분리 응답은 segments, 그 밖에는 text 하나."""
        items = payload.get('segments')
        if isinstance(items, list) and items:
            return [Segment(text=str(item.get('text') or '').strip(), speaker=item.get('speaker'),
                            start=item.get('start'), end=item.get('end'))
                    for item in items if isinstance(item, dict) and str(item.get('text') or '').strip()]
        text = payload.get('text')
        if not isinstance(text, str):
            raise TranscriptionError('STT_INVALID_RESPONSE', True)
        return [Segment(text=text.strip())] if text.strip() else []
