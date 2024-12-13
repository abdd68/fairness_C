import torch
import torch.nn as nn
import torch.optim as optim
from torchvision import datasets, transforms
import pandas as pd
import os
from torch.utils.data import DataLoader
from sklearn.model_selection import train_test_split
import numpy as np
from tqdm import tqdm
import argparse
from pytorch_grad_cam import GradCAM
from lib.resnet import Generator, Discriminator, Resnet18, Resnet50
from lib.vision_transformer import VisionTransformer
from import_datasets import MimicCXRDataset, CheXpertDataset, TcgaDataset
import logging
import time
from grad_rollout import VITAttentionGradRollout # !!!

# 检查 GPU 是否可用
os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3,4,5,6,7"
# 设置命令行参数

def process_dataset():
    if args.dataset == 'mimic-cxr':
        mimic_cxr_path = "../datasets/mimic-cxr/mimic-cxr-jpg-small/2.0.0"  # 修改为实际路径
        chexpert_csv_path = os.path.join(mimic_cxr_path, "metadata.csv")
        patients_csv_path = os.path.join(mimic_cxr_path, "patients.csv")
        
        # 读取 CSV 文件
        chexpert_df = pd.read_csv(chexpert_csv_path)
        patients_df = pd.read_csv(patients_csv_path)
        
        # 只选择需要的列，并确保包含 ViewPosition 信息
        chexpert_df = chexpert_df[["study_id", "subject_id", "ViewPosition", "Pneumonia"]]
        patients_df = patients_df[["subject_id", "gender"]]
        merged_df = pd.merge(chexpert_df, patients_df, on="subject_id")
        # 加入 ViewPosition 列，合并 chexpert_df 和 mimic-cxr-2.0.0-metadata.csv
        merged_df = merged_df[merged_df["ViewPosition"] == "PA"]
        merged_df['gender'] = merged_df['gender'].map({'M': 0, 'F': 1})
        
        if args.debug:
            train_metadata, test_metadata = train_test_split(merged_df, test_size=0.01, train_size = 0.09, random_state=args.seed)
        else:
            train_metadata, test_metadata = train_test_split(merged_df, test_size=0.1, train_size = 0.9, random_state=args.seed)
        train_dataset = MimicCXRDataset(metadata=train_metadata, root_dir=mimic_cxr_path, transform=transform)
        test_dataset = MimicCXRDataset(metadata=test_metadata, root_dir=mimic_cxr_path, transform=transform)
    elif args.dataset == 'chexpert':
        root_dir = '../datasets'
        train_dataset = CheXpertDataset(csv_file=os.path.join(root_dir, 'CheXpert-v1.0-small/train.csv'), 
                                root_dir=root_dir, 
                                transform=transform, debug=args.debug)
        test_dataset = CheXpertDataset(csv_file=os.path.join(root_dir, 'CheXpert-v1.0-small/valid.csv'), 
                               root_dir=root_dir, 
                               transform=transform, debug=False)
        
    elif args.dataset == 'tcga':
        root_dir = '../datasets/TCGA-LUAD/manifest-1Rd7jPNd5199284876140322680'
        train_dataset = TcgaDataset(csv_file=os.path.join(root_dir, 'metadata.csv'), root_dir=root_dir, transform=transform, debug=args.debug, training = True)
        test_dataset = TcgaDataset(csv_file=os.path.join(root_dir, 'metadata.csv'), root_dir=root_dir, transform=transform, debug=args.debug, training = False)
    # 创建 DataLoader
    train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True, num_workers=32)
    test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False, num_workers=32)

    logger.info(f"Number of samples in training dataset: {len(train_dataset)} | testing dataset:{len(test_dataset)}")
    
    return train_dataset, test_dataset, train_loader, test_loader

from pytorch_grad_cam.utils.model_targets import ClassifierOutputTarget

def generate_zone_masks_Y(images, labels):
    if args.model == 'resnet18' or args.model == 'resnet50':
        targets = [ClassifierOutputTarget(labels)]
        zone_mask = gradcam_Y(input_tensor=images, targets=targets)
    elif args.model == 'vit':
        zone_mask = gradcam_Y(images, labels).cpu().detach().numpy()
    return zone_mask

def generate_zone_masks_Z(images, labels):
    targets = [ClassifierOutputTarget(labels)]
    zone_mask = gradcam_Z(input_tensor=images, targets=targets)
    return zone_mask

def train_round(flag):
    model.train()
    G.eval()
    D.eval()
    
    total_loss = 0.0
    correct = 0
    total = 0
    
    # 使用 tqdm 包装 train_loader
    progress_bar = tqdm(train_loader, desc="Training")
    for images, target_labels, sensitive_labels in progress_bar:
        # 将数据移动到 GPU/CPU
        images = images.to(device)
        target_labels = target_labels.to(device)

        # 前向传播
        outputs = model(images)  # 提取特征
        if flag:
            print(outputs)
            print(target_labels)
            flag = 0
        # 计算损失
        loss = criterion_task(outputs, target_labels)
        
        # 反向传播和优化
        optimizer_model.zero_grad()
        loss.backward()
        optimizer_model.step()

        # 记录损失和准确率
        total_loss += loss.item()
        _, predicted = torch.max(outputs, 1)
        correct += (predicted == target_labels).sum().item()
        total += target_labels.size(0)
        
        # 计算平均损失并显示在 tqdm 中
        avg_loss = total_loss / total
        progress_bar.set_postfix({"Loss": loss.item(), "Avg Loss": avg_loss, "Accuracy": correct / total})
        
    final_loss = total_loss / len(train_loader)
    final_accuracy = correct / total
    logger.info(f"Final Training Loss: {final_loss:.4f}, Accuracy: {final_accuracy:.4f}")
    torch.save(model.state_dict(), f'models/model_{args.feature}.pth')

import matplotlib.pyplot as plt
import cv2

def show_mask_on_image(img, masky, masks):
    img_p = invTrans(img[0])
    img_heatbase = np.array(img_p.detach().cpu().squeeze().transpose(0,2).transpose(0,1))[:, :, ::-1]
    
    to_pil = transforms.ToPILImage()
    img_out = to_pil(img_p)
    img_out.save('images/output_image.png')
    
    masky, masks = masky.squeeze(), masks.squeeze()
    # masky
    heatmap = cv2.applyColorMap(np.uint8(255 * masky), cv2.COLORMAP_JET)
    heatmap = np.float32(heatmap) / 255
    cam = heatmap + np.float32(img_heatbase)
    cam = cam / np.max(cam)
    masked_image = np.uint8(255 * cam)
    cv2.imwrite(f'images/masked_output_y.png', masked_image)
    
    # masks
    heatmap = cv2.applyColorMap(np.uint8(255 * masks), cv2.COLORMAP_JET)
    heatmap = np.float32(heatmap) / 255
    cam = heatmap + np.float32(img_heatbase)
    cam = cam / np.max(cam)
    masked_image = np.uint8(255 * cam)
    cv2.imwrite(f'images/masked_output_s.png', masked_image)
    logger.info("Successfully output an image!")
    return 
    
def gen_mask(images, target_labels, sensitive_labels):
    zone_y = generate_zone_masks_Y(images[:1], int(target_labels[0]))
    zone_s = generate_zone_masks_Z(images[:1], 0)
    threshold_y = zone_y.max() * args.threshold
    threshold_s = zone_s.max() * args.threshold
    tripartite_mask_y = (zone_y > threshold_y)
    non_zone_y_mask = 1 - tripartite_mask_y
    tripartite_mask_s = (zone_s > threshold_s)
    tripartite_mask = non_zone_y_mask + 5 * tripartite_mask_s
    if args.visualize:
        show_mask_on_image(images, zone_y, zone_s)
    
    return tripartite_mask

def process_mask(noise, mask):
    mask = torch.tensor(mask, dtype=torch.float32)  # (1, 224, 224)
    mask = mask.unsqueeze(0).repeat(noise.shape[0], 3, 1, 1).to(device)  # 形状变为 (1, 1, 224, 224)
    return noise * mask

def visualize_noise(noise):
    # 如果 noise 是一个 Tensor，先将其移到 CPU 并转换为 NumPy 格式
    if isinstance(noise, torch.Tensor):
        noise = noise.detach().cpu().numpy()

    # 调整通道顺序从 [C, H, W] 到 [H, W, C]，用于 Matplotlib 显示
    
    noise = np.transpose(noise[0], (1, 2, 0))
    # 确保 noise 的值在 [0, 1] 范围内
    noise = (noise - noise.min()) / (noise.max() - noise.min())

    # 使用 Matplotlib 可视化
    plt.imshow(noise)
    plt.axis('off')
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)  # 去掉边缘空白
    plt.savefig('images/noise.png', bbox_inches='tight', pad_inches=0)
    logger.info("Successfully output the noise!")
    breakpoint()
    return
    
def adversarial_round(noise_strength=0.1):
    for i, (images, target_labels, sensitive_labels) in enumerate(train_loader):
        images = images.to(device)
        target_labels = target_labels.to(device)
        sensitive_labels = sensitive_labels.to(device)
        
        if not args.no_cam:
            tripartite_mask = gen_mask(images, target_labels, sensitive_labels)
        else:
            tripartite_mask = np.ones((1, 224, 224))
            
        if args.no_adversarial:
            perturbed_images = images
        else:
            noise = process_mask(G(images), tripartite_mask)
            if args.visualize:
                visualize_noise(noise)
                
            perturbed_images = torch.clamp(images * (1 - noise_strength) + noise * noise_strength, -4, 4)
            
        if not args.no_adversarial:
            # G -> model -> D round: Update (D)
            if i % 2 == 0:
                D.train()
                outputs_D = D(perturbed_images, target_labels)

                # 判别器的损失
                loss_D = criterion_fair(outputs_D, sensitive_labels.unsqueeze(1))
                
                # 更新判别器 D 的梯度
                optimizer_D.zero_grad()
                loss_D.backward()
                optimizer_D.step()
            
            # G -> model -> f/D round: Update (G), model, f
        
            G.train()
            noise = process_mask(G(images), tripartite_mask)  # 控制扰动强度
            perturbed_images = torch.clamp(images * (1 - noise_strength) + noise * noise_strength, -4, 4)
            predictions = model(perturbed_images)  # 提取扰动后的特征
            outputs_D = D(perturbed_images, target_labels)
            
            # 生成器的公平性损失
            entropy = - (outputs_D * torch.log(outputs_D + 1e-8) + (1 - outputs_D) * torch.log(1 - outputs_D + 1e-8))
            entropy_loss = torch.mean(entropy)
            L_G_fair = -criterion_fair(outputs_D, sensitive_labels.unsqueeze(1)) - alpha * entropy_loss
            
            # 分类损失（肺炎标签）
            L_G_Target = criterion_task(predictions, target_labels)
            L2_reg = sum((param ** 2).sum() for param in G.parameters())
            
            # 生成器的总损失
            L_G = L_G_fair + beta * L_G_Target + 1e-5 * L2_reg
            
            # 更新生成器 G, 特征提取器 model, 标签预测器 f 的梯度
            optimizer_G.zero_grad()
            L_G.backward()
            optimizer_G.step()
            
            # 打印训练信息
            if (i + 1) % 20 == 0:
                if args.no_adversarial:
                    logger.info(f"Step [{i+1}/{len(train_loader)}], Loss D: {loss_D.item():.4f}")
                else:
                    logger.info(f"Step [{i+1}/{len(train_loader)}], Loss D: {loss_D.item():.4f}, Loss G: {L_G.item():.4f}")
                    
from sklearn.metrics import f1_score
from sklearn.metrics import roc_auc_score
def acc_test_round(model, model_G, noise_strength=0.1):
    model.eval()  # 设置特征提取器为评估模式
    model_G.eval()  # 设置生成器为评估模式
    
    total_loss = 0.0
    correct = 0
    total = 0
    count_a, count_a_yhat1, count_a_y1, count_a_y0, count_a_y1_yhat1, count_a_y0_yhat0 = 0, 0, 0, 0, 0, 0 
    count_na, count_na_yhat1, count_na_y1, count_na_y0, count_na_y1_yhat1, count_na_y0_yhat0 = 0, 0, 0, 0, 0, 0 
    
    all_y_true, all_y_scores, all_y_predicted = [], [], []
    with torch.no_grad():  # 禁用梯度计算
        for images, target_labels, sensitive_labels in test_loader:
            # 将数据移动到指定设备（GPU或CPU）
            images = images.to(device)
            target_labels = target_labels.to(device)

            # 使用生成器 G 生成噪声并加到图像上
            if not args.no_adversarial:
                noise = model_G(images)  # 控制扰动强度
                perturbed_images = torch.clamp((1 - noise_strength) * images + noise_strength * noise, -4, 4)
            else:
                perturbed_images = images

            # 前向传播
            outputs = model(perturbed_images)  # 提取特征
            
            ## calculate fairness
            scores, indices = outputs.max(1)
            for i in range(images.shape[0]):
                if sensitive_labels[i] == 1:
                    count_a += 1
                    if indices[i] == 1:
                        count_a_yhat1 += 1
                        if target_labels[i] == 1:
                            count_a_y1_yhat1 += 1
                    if indices[i] == 0:
                        if target_labels[i] == 0:
                            count_a_y0_yhat0 += 1
                    if target_labels[i] == 1:
                        count_a_y1 += 1
                    if target_labels[i] == 0:
                        count_a_y0 += 1
                    
                else:
                    count_na += 1
                    if indices[i] == 1:
                        count_na_yhat1 += 1
                        if target_labels[i] == 1:
                            count_na_y1_yhat1 += 1
                    if indices[i] == 0:
                        if target_labels[i] == 0:
                            count_na_y0_yhat0 += 1
                    if target_labels[i] == 1:
                        count_na_y1 += 1
                    if target_labels[i] == 0:
                        count_na_y0 += 1
            ## end calculate fairness
            
            
            # 计算损失
            loss = criterion_task(outputs, target_labels)
            total_loss += loss.item()
            # 计算准确率
            _, predicted = torch.max(outputs, 1)  # 获得最大预测值的索引
            correct += (predicted == target_labels).sum().item()
            total += target_labels.size(0)
            
            y_scores = torch.softmax(outputs, dim=1)[:, 1]
            all_y_true.extend(target_labels.cpu().numpy())
            all_y_predicted.extend(predicted.cpu().numpy())
            all_y_scores.extend(y_scores.cpu().numpy())
    
    avg_loss = total_loss / len(test_loader)  # 计算平均损失
    accuracy = correct / total  # 计算准确率
    f1 = f1_score(all_y_true, all_y_predicted)
    auc = roc_auc_score(all_y_true, all_y_scores)

    logger.info(f"Test Loss: {avg_loss:.4f}, Accuracy: {accuracy:.4f}, F1: {f1:.4f}, AUC: {auc:.4f}")
    DP = abs(count_a_yhat1/count_a - count_na_yhat1/count_na)
    EO = abs(count_a_y1_yhat1/count_a_y1 - count_na_y1_yhat1/count_na_y1)
    logger.info(f"DP:{DP:.4f}, EO:{EO:.4f}")

    return avg_loss, accuracy
 
# def fairness_acc_test_round(noise_strength=0.1):
#     """
#     进行公平性测试，评估判别器在添加生成器生成的噪声后的表现。
#     """
#     fair_cls = Discriminator().to(device)
#     fair_cls.train()
#     optimizer_fair_cls = optim.Adam(fair_cls.parameters(), lr=args.lr)
    
#     for epoch in range(1):
#         for images, target_labels, sensitive_labels in tqdm(train_loader, desc='Fairness train Progress'):
#             images = images.to(device)
#             target_labels = target_labels.to(device)
#             sensitive_labels = sensitive_labels.to(device)

#             # 添加生成器生成的噪声
#             noise = G(images)   # 控制扰动强度
#             perturbed_images = torch.clamp(images * (1-noise_strength) + noise * noise_strength, -4, 4)

#             # 提取扰动后的特征
#             outputs_D = fair_cls(perturbed_images, target_labels).squeeze()
#             loss_D = criterion_fair(outputs_D, sensitive_labels)
            
#             optimizer_fair_cls.zero_grad()
#             loss_D.backward()
#             optimizer_fair_cls.step()
            
#     correct = 0
#     total = 0
#     fair_cls.eval()
#     with torch.no_grad():
#         for images, target_labels, sensitive_labels in tqdm(test_loader, desc='Fairness test Progress'):
#             images = images.to(device)
#             target_labels = target_labels.to(device)
#             sensitive_labels = sensitive_labels.to(device)

#             # 添加生成器生成的噪声
#             noise = G(images)   # 控制扰动强度
#             perturbed_images = torch.clamp(images * (1-noise_strength) + noise * noise_strength, -4, 4)

#             # 提取扰动后的特征
#             outputs_D = fair_cls(perturbed_images, target_labels).squeeze()

#             # 判别器的输出，用于预测性别标签
#             predicted = (outputs_D > 0.5).long()  # 使用 0.5 阈值进行二分类预测
#             correct += (predicted == sensitive_labels).sum().item()
#             total += sensitive_labels.size(0)

#     accuracy = correct / total
#     logger.info(f"Fairness Test Accuracy: {accuracy:.4f}")
    
#     return accuracy

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Adversarial Training with Fairness Testing')
    parser.add_argument('--device', type=str, default='cuda', help='Device to use for training (e.model., "cuda" or "cpu")')
    parser.add_argument('--alpha', type=float, default=0.8, help='Weight for the fairness loss term')
    parser.add_argument('--beta', type=float, default=1.0, help='Weight for the task loss term')
    parser.add_argument('-lr','--lr', type=float, default=1e-4, help='Weight for the task loss term')
    parser.add_argument('--pretrain_epochs', type=int, default=10, help='Number of epochs for training')
    parser.add_argument('--num_epochs', type=int, default=10, help='Number of epochs for training')

    parser.add_argument('--model', type=str, default='resnet18', help='model type, choose from resnet18, resnet50 and vit')
    parser.add_argument('-n', '--noise_strength', type=float, default=0.1, help='Strength of noise added by generator G')
    parser.add_argument('--no_adversarial', action='store_true', help='Enable adversarial training') # is false if not set
    parser.add_argument('-v', '--visualize', action='store_true', help='Enable visualization') # is false if not set
    parser.add_argument('--debug', action='store_true', help='Enable adversarial training') # is false if not set
    parser.add_argument('--no_cam', action='store_true', help='Enable adversarial training') # is false if not set
    parser.add_argument('-s','--seed', type=int, default=42, help='seed used for training')
    parser.add_argument('--threshold', type=float, default=0.5, help='threshold for masks')
    parser.add_argument('-pr', '--positive_weight', type=float, default=1., help='positive_weight')
    parser.add_argument('-d', '--dataset', type=str, default='mimic-cxr', help = 'choose from mimic-cxr, chexpert and tcga')
    parser.add_argument('-f', '--feature', type=str, default='test', help = 'choose from mimic-cxr, chexpert and tcga')
    args = parser.parse_args()

    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO,  
                        filename='logs/' + args.feature +'_'+ time.strftime('%m-%d-%H:%M:%S',time.localtime(time.time())) + '.log',
                        filemode='w',  
                        format='%(asctime)s: %(message)s'
                        )
    logger.info(args)
    # 设置设备
    device = torch.device(args.device if torch.cuda.is_available() else "cpu")
    num_gpus = torch.cuda.device_count()
    logger.info(f"Number of GPUs available: {num_gpus}")

    # 设置参数
    alpha = args.alpha
    beta = args.beta
    noise_strength = args.noise_strength

    # 数据预处理和加载
    transform = transforms.Compose([
        transforms.Resize((224, 224)),  # 将图像大小调整为 224x224
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]) # 标准化
    ])
    invTrans = transforms.Compose([ transforms.Normalize(mean = [ 0., 0., 0. ],
                                                        std = [ 1/0.229, 1/0.224, 1/0.225 ]),
                                    transforms.Normalize(mean = [ -0.485, -0.456, -0.406 ],
                                                        std = [ 1., 1., 1. ]),])
    # 模型初始化并移动到 GPU（如果可用），并使用 DataParallel 包裹模型
    if args.model == 'resnet18':
        model = Resnet18(num_classes = 2).to(device)
    elif args.model == 'resnet50':
        model = Resnet50(num_classes = 2).to(device)
    elif args.model == 'vit':
        model = VisionTransformer(depth = 1, num_classes = 2).to(device)
    G = Generator().to(device)
    D = Discriminator().to(device)

    if args.model == 'resnet18':
        gradcam_Y = GradCAM(model=model, target_layers=[model.layer4[1].conv2])
        gradcam_Z = GradCAM(model=D, target_layers=[D.conv2])
    elif args.model == 'resnet50':
        gradcam_Y = GradCAM(model=model, target_layers=[model.layer4[2].conv3])
        gradcam_Z = GradCAM(model=D, target_layers=[D.conv2])
    elif args.model == 'vit':
        gradcam_Y = VITAttentionGradRollout(model, discard_ratio=0.9)
        gradcam_Z = GradCAM(model=D, target_layers=[D.conv2])

    # torch.backends.cudnn.enabled = False
    # 如果有多张 GPU，使用 DataParallel 包裹模型
    if num_gpus > 1:
        model = nn.DataParallel(model)
        G = nn.DataParallel(G)

    if args.pretrain_epochs <= 0:
        model.load_state_dict(torch.load(f'models/model_{args.feature}.pth'))
        logger.info("Successfully load model!")
    train_dataset, test_dataset, train_loader, test_loader = process_dataset()
    # 损失函数和优化器
    pw = args.positive_weight * train_dataset.calculate_neg_pos_ratio()
    class_weights = torch.tensor([1.0, pw]).to(device)  # 权重为 [阴性, 阳性]
    logger.info(f"positive weight: {pw:.4f}")
    criterion_task = nn.CrossEntropyLoss(weight=class_weights)
    criterion_fair = nn.BCELoss()

    optimizer_G = optim.AdamW(G.parameters(), lr=1.1 * args.lr, weight_decay=1e-4)
    optimizer_D = optim.AdamW(D.parameters(), lr=args.lr, weight_decay=1e-4)
    if args.model == 'resnet18' or args.model == 'resnet50':
        optimizer_model = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    elif args.model == 'vit':
        optimizer_model = optim.AdamW(model.parameters(), lr=args.lr)

    scheduler_G = optim.lr_scheduler.StepLR(optimizer_G, step_size=10, gamma=0.5)
    scheduler_D = optim.lr_scheduler.StepLR(optimizer_D, step_size=10, gamma=0.5)
    scheduler_model = optim.lr_scheduler.StepLR(optimizer_model, step_size=10, gamma=0.5)
    for epoch in range(args.pretrain_epochs):
        flag = 0
        if epoch >= 1:
            flag = 1
        train_round(flag)
        scheduler_model.step()
        
    acc_test_round(model, G, noise_strength)
    for epoch in range(args.num_epochs):
        adversarial_round(noise_strength)
        acc_test_round(model, G, noise_strength)
        scheduler_G.step()
        scheduler_D.step()

        # fairness_acc_test_round(noise_strength)
        

    logger.info("Training completed.")