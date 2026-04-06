from transformers import AutoModelForImageTextToText, AutoProcessor
from PIL import Image
import torch

# 使用相对路径 ./model
model_name = "./model"

# 加载处理器
processor = AutoProcessor.from_pretrained(model_name)

# 使用新的类名加载多模态模型
model = AutoModelForImageTextToText.from_pretrained(
    model_name,
    torch_dtype="auto",
    device_map="auto"
)

# 加载本地图片
image_path = "./image.png"  # 替换为你的图片路径
image = Image.open(image_path)

# 准备对话
prompt = "请描述这张图片中的内容。"
messages = [
    {
        "role": "user", 
        "content": [
            {"type": "image"},  # 图片占位符
            {"type": "text", "text": prompt}
        ]
    }
]

# 应用聊天模板
text = processor.apply_chat_template(
    messages,
    tokenize=False,
    add_generation_prompt=True
)

# 处理输入（同时处理文本和图片）
inputs = processor(
    text=[text],
    images=[image],
    return_tensors="pt"
).to(model.device)

# 生成回复
generated_ids = model.generate(
    **inputs,
    max_new_tokens=512,
    top_k=20,
    top_p=1.00,
    temperature=1.0,
    do_sample=True,
    repetition_penalty=1.0
)

# 只保留新生成的token
generated_ids = [
    output_ids[len(input_ids):] 
    for input_ids, output_ids in zip(inputs.input_ids, generated_ids)
]

# 解码输出
response = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
print(response)