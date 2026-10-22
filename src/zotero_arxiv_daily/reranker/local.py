from .base import BaseReranker, register_reranker
import logging
import time
import warnings
import numpy as np
from loguru import logger
@register_reranker("local")
class LocalReranker(BaseReranker):
    def get_similarity_score(self, s1: list[str], s2: list[str]) -> np.ndarray:
        from sentence_transformers import SentenceTransformer
        if not self.config.executor.debug:
            from transformers.utils import logging as transformers_logging
            from huggingface_hub.utils import logging as hf_logging
    
            transformers_logging.set_verbosity_error()
            hf_logging.set_verbosity_error()
            logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
            logging.getLogger("sentence_transformers.SentenceTransformer").setLevel(logging.ERROR)
            logging.getLogger("transformers").setLevel(logging.ERROR)
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
            warnings.filterwarnings("ignore", category=FutureWarning)

        # 分三段计时：建模型（含下载/加载）和两次编码在 CPU 上不是一个量级，混着看不出瓶颈
        started = time.perf_counter()
        encoder = SentenceTransformer(self.config.reranker.local.model, trust_remote_code=True)
        logger.info(f"[timing] load reranker model: {time.perf_counter() - started:.1f}s")
        if self.config.reranker.local.encode_kwargs:
            encode_kwargs = self.config.reranker.local.encode_kwargs
        else:
            encode_kwargs = {}
        started = time.perf_counter()
        s1_feature = encoder.encode(s1,**encode_kwargs,show_progress_bar=True)
        logger.info(f"[timing] encode {len(s1)} candidate texts: {time.perf_counter() - started:.1f}s")
        started = time.perf_counter()
        s2_feature = encoder.encode(s2,**encode_kwargs,show_progress_bar=True)
        logger.info(f"[timing] encode {len(s2)} corpus texts: {time.perf_counter() - started:.1f}s")
        sim = encoder.similarity(s1_feature, s2_feature)
        return sim.numpy()