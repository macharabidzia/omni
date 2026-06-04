Qwen3-Omni
Chat
Overview
Introduction



Qwen3-Omni is the natively end-to-end multilingual omni-modal foundation models. It processes text, images, audio, and video, and delivers real-time streaming responses in both text and natural speech. We introduce several architectural upgrades to improve performance and efficiency. Key features:

State-of-the-art across modalities: Early text-first pretraining and mixed multimodal training provide native multimodal support. While achieving strong audio and audio-video results, unimodal text and image performance does not regress. Reaches SOTA on 22 of 36 audio/video benchmarks and open-source SOTA on 32 of 36; ASR, audio understanding, and voice conversation performance is comparable to Gemini 2.5 Pro.

Multilingual: Supports 119 text languages, 19 speech input languages, and 10 speech output languages.

Speech Input: English, Chinese, Korean, Japanese, German, Russian, Italian, French, Spanish, Portuguese, Malay, Dutch, Indonesian, Turkish, Vietnamese, Cantonese, Arabic, Urdu.
Speech Output: English, Chinese, French, German, Russian, Italian, Spanish, Portuguese, Japanese, Korean.
Novel Architecture: MoE-based Thinker–Talker design with AuT pretraining for strong general representations, plus a multi-codebook design that drives latency to a minimum.

Real-time Audio/Video Interaction: Low-latency streaming with natural turn-taking and immediate text or speech responses.

Flexible Control: Customize behavior via system prompts for fine-grained control and easy adaptation.

Detailed Audio Captioner: Qwen3-Omni-30B-A3B-Captioner is now open source: a general-purpose, highly detailed, low-hallucination audio captioning model that fills a critical gap in the open-source community.

Model Architecture



Cookbooks for Usage Cases
Qwen3-Omni supports a wide range of multimodal application scenarios, covering various domain tasks involving audio, image, video, and audio-visual modalities. Below are several cookbooks demonstrating the usage cases of Qwen3-Omni and these cookbooks include our actual execution logs. You can first follow the QuickStart guide to download the model and install the necessary inference environment dependencies, then run and experiment locally—try modifying prompts or switching model types, and enjoy exploring the capabilities of Qwen3-Omni!

Category	Cookbook	Description	Open
Audio	Speech Recognition	Speech recognition, supporting multiple languages and long audio.	Open In Colab
Speech Translation	Speech-to-Text / Speech-to-Speech translation.	Open In Colab
Music Analysis	Detailed analysis and appreciation of any music, including style, genre, rhythm, etc.	Open In Colab
Sound Analysis	Description and analysis of various sound effects and audio signals.	Open In Colab
Audio Caption	Audio captioning, detailed description of any audio input.	Open In Colab
Mixed Audio Analysis	Analysis of mixed audio content, such as speech, music, and environmental sounds.	Open In Colab
Visual	OCR	OCR for complex images.	Open In Colab
Object Grounding	Target detection and grounding.	Open In Colab
Image Question	Answering arbitrary questions about any image.	Open In Colab
Image Math	Solving complex mathematical problems in images, highlighting the capabilities of the Thinking model.	Open In Colab
Video Description	Detailed description of video content.	Open In Colab
Video Navigation	Generating navigation commands from first-person motion videos.	Open In Colab
Video Scene Transition	Analysis of scene transitions in videos.	Open In Colab
Audio-Visual	Audio Visual Question	Answering arbitrary questions in audio-visual scenarios, demonstrating the model's ability to model temporal alignment between audio and video.	Open In Colab
Audio Visual Interaction	Interactive communication with the model using audio-visual inputs, including task specification via audio.	Open In Colab
Audio Visual Dialogue	Conversational interaction with the model using audio-visual inputs, showcasing its capabilities in casual chat and assistant-like behavior.	Open In Colab
Agent	Audio Function Call	Using audio input to perform function calls, enabling agent-like behaviors.	Open In Colab
Downstream Task Fine-tuning	Omni Captioner	Introduction and capability demonstration of Qwen3-Omni-30B-A3B-Captioner, a downstream fine-tuned model based on Qwen3-Omni-30B-A3B-Instruct, illustrating the strong generalization ability of the Qwen3-Omni foundation model.	Open In Colab
QuickStart
Model Description and Download
Below is the description of all Qwen3-Omni models. Please select and download the model that fits your needs.

Model Name	Description
Qwen3-Omni-30B-A3B-Instruct	The Instruct model of Qwen3-Omni-30B-A3B, containing both thinker and talker, supporting audio, video, and text input, with audio and text output. For more information, please read the Qwen3-Omni Technical Report.
Qwen3-Omni-30B-A3B-Thinking	The Thinking model of Qwen3-Omni-30B-A3B, containing the thinker component, equipped with chain-of-thought reasoning, supporting audio, video, and text input, with text output. For more information, please read the Qwen3-Omni Technical Report.
Qwen3-Omni-30B-A3B-Captioner	A downstream audio fine-grained caption model fine-tuned from Qwen3-Omni-30B-A3B-Instruct, which produces detailed, low-hallucination captions for arbitrary audio inputs. It contains the thinker, supporting audio input and text output. For more information, you can refer to the model's cookbook.
During loading in Hugging Face Transformers or vLLM, model weights will be automatically downloaded based on the model name. However, if your runtime environment is not conducive to downloading weights during execution, you can refer to the following commands to manually download the model weights to a local directory:

# Download through ModelScope (recommended for users in Mainland China)
pip install -U modelscope
modelscope download --model Qwen/Qwen3-Omni-30B-A3B-Instruct --local_dir ./Qwen3-Omni-30B-A3B-Instruct
modelscope download --model Qwen/Qwen3-Omni-30B-A3B-Thinking --local_dir ./Qwen3-Omni-30B-A3B-Thinking
modelscope download --model Qwen/Qwen3-Omni-30B-A3B-Captioner --local_dir ./Qwen3-Omni-30B-A3B-Captioner

# Download through Hugging Face
pip install -U "huggingface_hub[cli]"
huggingface-cli download Qwen/Qwen3-Omni-30B-A3B-Instruct --local-dir ./Qwen3-Omni-30B-A3B-Instruct
huggingface-cli download Qwen/Qwen3-Omni-30B-A3B-Thinking --local-dir ./Qwen3-Omni-30B-A3B-Thinking
huggingface-cli download Qwen/Qwen3-Omni-30B-A3B-Captioner --local-dir ./Qwen3-Omni-30B-A3B-Captioner

Transformers Usage
Installation
The Hugging Face Transformers code for Qwen3-Omni has been successfully merged, but the PyPI package has not yet been released. Therefore, you need to install it from source using the following command. We strongly recommend that you create a new Python environment to avoid environment runtime issues.

# If you already have transformers installed, please uninstall it first, or create a new Python environment
# pip uninstall transformers
pip install git+https://github.com/huggingface/transformers
pip install accelerate

We offer a toolkit to help you handle various types of audio and visual input more conveniently, providing an API-like experience. This includes support for base64, URLs, and interleaved audio, images, and videos. You can install it using the following command and make sure your system has ffmpeg installed:

pip install qwen-omni-utils -U

Additionally, we recommend using FlashAttention 2 when running with Hugging Face Transformers to reduce GPU memory usage. However, if you are primarily using vLLM for inference, this installation is not necessary, as vLLM includes FlashAttention 2 by default.

pip install -U flash-attn --no-build-isolation

Also, you should have hardware that is compatible with FlashAttention 2. Read more about it in the official documentation of the FlashAttention repository. FlashAttention 2 can only be used when a model is loaded in torch.float16 or torch.bfloat16.

Code Snippet
Here is a code snippet to show you how to use Qwen3-Omni with transformers and qwen_omni_utils:

import soundfile as sf

from transformers import Qwen3OmniMoeForConditionalGeneration, Qwen3OmniMoeProcessor
from qwen_omni_utils import process_mm_info

MODEL_PATH = "Qwen/Qwen3-Omni-30B-A3B-Instruct"
# MODEL_PATH = "Qwen/Qwen3-Omni-30B-A3B-Thinking"

model = Qwen3OmniMoeForConditionalGeneration.from_pretrained(
    MODEL_PATH,
    dtype="auto",
    device_map="auto",
    attn_implementation="flash_attention_2",
)

processor = Qwen3OmniMoeProcessor.from_pretrained(MODEL_PATH)

conversation = [
    {
        "role": "user",
        "content": [
            {"type": "image", "image": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-Omni/demo/cars.jpg"},
            {"type": "audio", "audio": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-Omni/demo/cough.wav"},
            {"type": "text", "text": "What can you see and hear? Answer in one short sentence."}
        ],
    },
]

# Set whether to use audio in video
USE_AUDIO_IN_VIDEO = True

# Preparation for inference
text = processor.apply_chat_template(conversation, add_generation_prompt=True, tokenize=False)
audios, images, videos = process_mm_info(conversation, use_audio_in_video=USE_AUDIO_IN_VIDEO)
inputs = processor(text=text, 
                   audio=audios, 
                   images=images, 
                   videos=videos, 
                   return_tensors="pt", 
                   padding=True, 
                   use_audio_in_video=USE_AUDIO_IN_VIDEO)
inputs = inputs.to(model.device).to(model.dtype)

# Inference: Generation of the output text and audio
text_ids, audio = model.generate(**inputs, 
                                 speaker="Ethan", 
                                 thinker_return_dict_in_generate=True,
                                 use_audio_in_video=USE_AUDIO_IN_VIDEO)

text = processor.batch_decode(text_ids.sequences[:, inputs["input_ids"].shape[1] :],
                              skip_special_tokens=True,
                              clean_up_tokenization_spaces=False)
print(text)
if audio is not None:
    sf.write(
        "output.wav",
        audio.reshape(-1).detach().cpu().numpy(),
        samplerate=24000,
    )

Here are some more advanced usage examples. You can expand the sections below to learn more.

Batch inference
Use audio output or not
vLLM Usage
Installation
We strongly recommend using vLLM for inference and deployment of the Qwen3-Omni series models. Since our code is currently in the pull request stage, and audio output inference support for the Instruct model will be released in the near future, you can follow the commands below to install vLLM from source. Please note that we recommend you create a new Python environment to avoid runtime environment conflicts and incompatibilities. For more details on compiling vLLM from source, please refer to the vLLM official documentation.

git clone -b qwen3_omni https://github.com/wangxiongts/vllm.git
cd vllm
pip install -r requirements/build.txt
pip install -r requirements/cuda.txt
export VLLM_PRECOMPILED_WHEEL_LOCATION=https://wheels.vllm.ai/a5dd03c1ebc5e4f56f3c9d3dc0436e9c582c978f/vllm-0.9.2-cp38-abi3-manylinux1_x86_64.whl
VLLM_USE_PRECOMPILED=1 pip install -e . -v --no-build-isolation
# If you meet an "Undefined symbol" error while using VLLM_USE_PRECOMPILED=1, please use "pip install -e . -v" to build from source.
# Install the Transformers
pip install git+https://github.com/huggingface/transformers
pip install accelerate
pip install qwen-omni-utils -U
pip install -U flash-attn --no-build-isolation

Inference
You can use the following code for vLLM inference. The limit_mm_per_prompt parameter specifies the maximum number of each modality's data allowed per message. Since vLLM needs to pre-allocate GPU memory, larger values will require more GPU memory; if OOM issues occur, try reducing this value. Setting tensor_parallel_size greater than one enables multi-GPU parallel inference, improving concurrency and throughput. In addition, max_num_seqs indicates the number of sequences that vLLM processes in parallel during each inference step. A larger value requires more GPU memory but enables higher batch inference speed. For more details, please refer to the vLLM official documentation. Below is a simple example of how to run Qwen3-Omni with vLLM:

import os
import torch

from vllm import LLM, SamplingParams
from transformers import Qwen3OmniMoeProcessor
from qwen_omni_utils import process_mm_info

if __name__ == '__main__':
    # vLLM engine v1 not supported yet
    os.environ['VLLM_USE_V1'] = '0'

    MODEL_PATH = "Qwen/Qwen3-Omni-30B-A3B-Instruct"
    # MODEL_PATH = "Qwen/Qwen3-Omni-30B-A3B-Thinking"

    llm = LLM(
            model=MODEL_PATH, trust_remote_code=True, gpu_memory_utilization=0.95,
            tensor_parallel_size=torch.cuda.device_count(),
            limit_mm_per_prompt={'image': 3, 'video': 3, 'audio': 3},
            max_num_seqs=8,
            max_model_len=32768,
            seed=1234,
    )

    sampling_params = SamplingParams(
        temperature=0.6,
        top_p=0.95,
        top_k=20,
        max_tokens=16384,
    )

    processor = Qwen3OmniMoeProcessor.from_pretrained(MODEL_PATH)

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "video", "video": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-Omni/demo/draw.mp4"}
            ], 
        }
    ]

    text = processor.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    audios, images, videos = process_mm_info(messages, use_audio_in_video=True)

    inputs = {
        'prompt': text,
        'multi_modal_data': {},
        "mm_processor_kwargs": {
            "use_audio_in_video": True,
        },
    }

    if images is not None:
        inputs['multi_modal_data']['image'] = images
    if videos is not None:
        inputs['multi_modal_data']['video'] = videos
    if audios is not None:
        inputs['multi_modal_data']['audio'] = audios

    outputs = llm.generate([inputs], sampling_params=sampling_params)

    print(outputs[0].outputs[0].text)

Here are some more advanced usage examples. You can expand the sections below to learn more.

Batch inference
vLLM Serve Usage
Usage Tips (Recommended Reading)
Minimum GPU memory requirements
Model	Precision	15s Video	30s Video	60s Video	120s Video
Qwen3-Omni-30B-A3B-Instruct	BF16	78.85 GB	88.52 GB	107.74 GB	144.81 GB
Qwen3-Omni-30B-A3B-Thinking	BF16	68.74 GB	77.79 GB	95.76 GB	131.65 GB
Note: The table above presents the theoretical minimum memory requirements for inference with transformers and BF16 precision, tested with attn_implementation="flash_attention_2". The Instruct model includes both the thinker and talker components, whereas the Thinking model includes only the thinker part.

Prompt for Audio-Visual Interaction
When using Qwen3-Omni for audio-visual multimodal interaction, where the input consists of a video and its corresponding audio (with the audio serving as a query), we recommend using the following system prompt. This setup helps the model maintain high reasoning capability while better assuming interactive roles such as a smart assistant. Additionally, the text generated by the thinker will be more readable, with a natural, conversational tone and without complex formatting that is difficult to vocalize, leading to more stable and fluent audio output from the talker. You can customize the user_system_prompt field in the system prompt to include character settings or other role-specific descriptions as needed.

user_system_prompt = "You are Qwen-Omni, a smart voice assistant created by Alibaba Qwen."
message = {
    "role": "system",
    "content": [
          {"type": "text", "text": f"{user_system_prompt} You are a virtual voice assistant with no gender or age.\nYou are communicating with the user.\nIn user messages, “I/me/my/we/our” refer to the user and “you/your” refer to the assistant. In your replies, address the user as “you/your” and yourself as “I/me/my”; never mirror the user’s pronouns—always shift perspective. Keep original pronouns only in direct quotes; if a reference is unclear, ask a brief clarifying question.\nInteract with users using short(no more than 50 words), brief, straightforward language, maintaining a natural tone.\nNever use formal phrasing, mechanical expressions, bullet points, overly structured language. \nYour output must consist only of the spoken content you want the user to hear. \nDo not include any descriptions of actions, emotions, sounds, or voice changes. \nDo not use asterisks, brackets, parentheses, or any other symbols to indicate tone or actions. \nYou must answer users' audio or text questions, do not directly describe the video content. \nYou should communicate in the same language strictly as the user unless they request otherwise.\nWhen you are uncertain (e.g., you can't see/hear clearly, don't understand, or the user makes a comment rather than asking a question), use appropriate questions to guide the user to continue the conversation.\nKeep replies concise and conversational, as if talking face-to-face."}
    ]
}

Best Practices for the Thinking Model
The Qwen3-Omni-30B-A3B-Thinking model is primarily designed for understanding and interacting with multimodal inputs, including text, audio, image, and video. To achieve optimal performance, we recommend that users include an explicit textual instruction or task description in each round of dialogue alongside the multimodal input. This helps clarify the intent and significantly enhances the model's ability to leverage its reasoning capabilities. For example:

messages = [
    {
        "role": "user",
        "content": [
            {"type": "audio", "audio": "/path/to/audio.wav"},
            {"type": "image", "image": "/path/to/image.png"},
            {"type": "video", "video": "/path/to/video.mp4"},
            {"type": "text", "text": "Analyze this audio, image, and video together."},
        ], 
    }
]

Use audio in video
In multimodal interaction, user-provided videos are often accompanied by audio (such as spoken questions or sounds from events in the video). This information helps the model provide a better interactive experience. We provide the following options for users to decide whether to use the audio from a video.

# In data preprocessing
audios, images, videos = process_mm_info(messages, use_audio_in_video=True)

# For Transformers
text = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
inputs = processor(text=text, audio=audios, images=images, videos=videos, return_tensors="pt", 
                   padding=True, use_audio_in_video=True)
text_ids, audio = model.generate(..., use_audio_in_video=True)

# For vLLM
text = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
inputs = {
    'prompt': text,
    'multi_modal_data': {},
    "mm_processor_kwargs": {
        "use_audio_in_video": True,
    },
}

It is worth noting that during a multi-round conversation, the use_audio_in_video parameter must be set consistently across these steps; otherwise, unexpected results may occur.

Evaluation
Performance of Qwen3-Omni
Qwen3-Omni maintains state-of-the-art performance on text and visual modalities without degradation relative to same-size single-model Qwen counterparts. Across 36 audio and audio-visual benchmarks, it achieves open-source SOTA on 32 and sets the SOTA on 22, outperforming strong closed-source systems such as Gemini 2.5 Pro and GPT-4o.

Text -> Text
Audio -> Text
Vision -> Text
AudioVisual -> Text
Zero-shot Speech Generation
Multilingual Speech Generation
Cross-Lingual Speech Generation
Setting for Evaluation
Decoding Strategy: For the Qwen3-Omni series across all evaluation benchmarks, Instruct models use greedy decoding during generation without sampling. For Thinking models, the decoding parameters should be taken from the generation_config.json file in the checkpoint.
Benchmark-Specific Formatting: For the majority of evaluation benchmarks, they come with their own ChatML formatting to embed the question or prompt. It should be noted that all video data are set to fps=2 during evaluation.
Default Prompts: For tasks in certain benchmarks that do not include a prompt, we use the following prompt settings:
Task Type	Prompt
Auto Speech Recognition (ASR) for Chinese	请将这段中文语音转换为纯文本。
Auto Speech Recognition (ASR) for Other languages	Transcribe the audio into text.
Speech-to-Text Translation (S2TT)	Listen to the provided speech and produce a translation in text.
Song Lyrics Recognition	Transcribe the song lyrics into text without any punctuation, separate lines with line breaks, and output only the lyrics without additional explanations.
System Prompt: No system prompt should be set for any evaluation benchmark.
Input Sequence: The question or prompt should be input as user text. Unless otherwise specified by the benchmark, the text should come after multimodal data in the sequence. For example:
messages = [
    {
        "role": "user",
        "content": [
            {"type": "audio", "audio": "/path/to/audio.wav"},
            {"type": "image", "image": "/path/to/image.png"},
            {"type": "video", "video": "/path/to/video.mp4"},
            {"type": "text", "text": "Describe the audio, image and video."},
        ],
    },
]

HTTP services
Connect to your service using HTTP using a proxied domain and port

Port 5173

HTTP Service

Port 8000

HTTP Service

SSH
Connect to your Pod using SSH. (No support for SCP & SFTP)

$
ssh 7lycxebyx5swhu-64411390@ssh.runpod.io -i ~/.ssh/id_ed25519
SSH over exposed TCP
Connect to your Pod using SSH over a direct TCP connection. (Supports SCP & SFTP)

$
ssh root@195.26.233.28 -p 58614 -i ~/.ssh/id_ed25519
Web terminal
Connect to your Pod using a terminal directly in your browser


Enable web terminal

Direct TCP ports
Connect to your Pod using direct TCP connections to exposed ports.

195.26.233.28:58614

:22

https://7lycxebyx5swhu-5173.proxy.runpod.net/