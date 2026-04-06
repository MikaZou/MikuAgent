from transformers import AutoModelForCausalLM, AutoTokenizer

# 使用相对路径 ./model
model_name = "./model"  # 假设模型文件在当前目录的model文件夹下

# 加载模型（修正参数名）
model = AutoModelForCausalLM.from_pretrained(
    model_name,
    torch_dtype="auto",  # 修正：dtype -> torch_dtype
    device_map="auto"
)

# 加载分词器
tokenizer = AutoTokenizer.from_pretrained(model_name)

# 准备对话
prompt = "请介绍一下你自己。"
messages = [
    {"role": "user", "content": prompt}
]

# 应用聊天模板（移除可能有兼容性问题的参数）
text = tokenizer.apply_chat_template(
    messages,
    tokenize=False,
    add_generation_prompt=True
    # enable_thinking=False  # 如果不支持，注释掉
)

# 编码输入
model_inputs = tokenizer([text], return_tensors="pt").to(model.device)

# 生成回复
generated_ids = model.generate(
    **model_inputs,
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
    for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
]

# 解码输出
response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
print(response)