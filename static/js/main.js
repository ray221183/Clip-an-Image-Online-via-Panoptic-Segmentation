//////////////////////////////
//last updated date: Sep. 23, 2020
//////////////////////////////
//////////////////////////////

let imageSourceList = [ "https://tinyurl.com/y5nfrbcb",//"https://media.tacdn.com/media/attractions-splice-spp-674x446/07/03/1c/9c.jpg",
                        "https://tinyurl.com/y4nv6omq",//"https://image.cdn-eztravel.com.tw/VJq3NXITDcuZ7Fu_9HWU3N1n7iOwNFiNF2aGiL8i-MU/rs:fill:600:315:1/g:ce/aHR0cHM6Ly92YWNhdGlvbi5jZG4tZXp0cmF2ZWwuY29tLnR3L2ltZy9WRFIvRlJfNTM4MDk5NTkxLmpwZw.jpg",
                        "https://tinyurl.com/y2w7lvqh",//"https://tripineuandau.files.wordpress.com/2018/08/11111.jpg?w=850&h=500&crop=1",
                        "https://tinyurl.com/yxfbsssq",//"https://img.itinari.com/pages/images/original/f49a1ac0-39f8-4f9b-b0f0-fc43181afd4e-istock-821594018.jpg?ch=DPR&dpr=1&w=1600&s=0c1247139da5a3ab211f2bd4ba8eaa48",
                        "https://tinyurl.com/yyq9v2jg",//"https://muda-kompas-id.azureedge.net/wp-content/uploads/sites/31/2016/07/marina-bay-hote-murah-singapore.jpg",
                        "https://tinyurl.com/y2cgxlf6",//"https://i.pinimg.com/originals/87/5a/39/875a3964218f776063a82a40f7989c41.jpg",
                        "https://tinyurl.com/yy8zk47e",//"https://memeprod.sgp1.digitaloceanspaces.com/user-template/7f8b089e7b96f012a559d89922125603.png",
                        "https://tinyurl.com/yy7qs4jj",//"https://i.imgur.com/q75wNWm.jpg",
                        "https://tinyurl.com/y289l8pv",//"https://memeprod.sgp1.digitaloceanspaces.com/meme/23bcb062cc34426ee2e3ca530c3ea427.png",
                        "https://tinyurl.com/y6x9ls26",//"https://memeprod.s3.ap-northeast-1.amazonaws.com/user-wtf/1575381417321.jpg",
                        "https://tinyurl.com/yyl57msk",//"https://i.imgur.com/2MkcJZx.jpg",
                        "https://tinyurl.com/yyblo6re",//"https://i.imgur.com/MgZC4Ril.jpg",
                        "https://tinyurl.com/y5hfwoya"]//"https://i.imgur.com/dhombjF.jpg"]
let linkNameList = ["Paris", "Nice", "Germany", "Hallstatt", "Singapore", "Cappadocia", "Patrick1", "Patrick2", "Patrick3", "Patrick4", "Patrick5", "Patrick6", "PatrickEnd"]
let imageIndex = 1;
let image = document.getElementById("display");
let linkName = document.getElementById("source");
let previous = document.getElementById("previous");
let next = document.getElementById("next");
// image.onload = function(){
//     image.src = imageSourceList[imageIndex]
// }
linkName.innerText = linkNameList[imageIndex];
linkName.href = imageSourceList[imageIndex];

document.addEventListener("DOMContentLoaded", function(){
    if(imageIndex <= 0){
        previous.parentElement.className = "disabled";
    }
    if(imageIndex >= imageSourceList.length - 1){
        next.parentElement.className = "disabled";
    }
})


function changeImage( dir ){ //true: next image, false: previous image
    console.log("11");
    imageIndex = dir ? imageIndex + 1 : imageIndex - 1;
    if(imageIndex <= 0){
        imageIndex = 0;
        previous.parentElement.className = "disabled";
    }
    else{
        previous.parentElement.className = "image-viewer__button";
    }
    if(imageIndex >= imageSourceList.length - 1){
        imageIndex = imageSourceList.length - 1;
        next.parentElement.className = "disabled";
    }
    else{
        next.parentElement.className = "image-viewer__button";
    }
    changeImageSrc();
    changeLink();
}
function changeImageSrc(){
    image.src = "/../images/loading.gif";
}
function changeLink(){
    linkName.innerText = linkNameList[imageIndex];
    linkName.href = imageSourceList[imageIndex];
}

function readURL(input) {
    console.log(1)
    if (input.files && input.files[0]) {
      var reader = new FileReader();
      
      reader.onload = function(e) {
        image.src = String(e.target.result);
      }
      reader.readAsDataURL(input.files[0]); // convert to base64 string
    }
}
