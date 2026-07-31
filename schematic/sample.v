// https://github.com/pulp-platform/axi2mem/blob/master/axi2mem.sv
module axi2mem
#(
   parameter PER_ADDR_WIDTH = 32,
   parameter PER_ID_WIDTH   = 5,
   parameter AXI_ADDR_WIDTH = 32,
   parameter AXI_DATA_WIDTH = 64,
   parameter AXI_USER_WIDTH = 6,
   parameter AXI_ID_WIDTH   = 3,
   parameter BUFFER_DEPTH   = 2,
   parameter AXI_STRB_WIDTH = AXI_DATA_WIDTH/8
)
(
   input  logic                      clk_i,
   input  logic                      rst_ni,
   input  logic                      test_en_i,

   // AXI4 SLAVE
   //***************************************
   // WRITE ADDRESS CHANNEL
   input  logic                      axi_slave_aw_valid_i,
   input  logic [AXI_ADDR_WIDTH-1:0] axi_slave_aw_addr_i,
   input  logic [2:0]                axi_slave_aw_prot_i,
   input  logic [3:0]                axi_slave_aw_region_i,
   input  logic [7:0]                axi_slave_aw_len_i,
   input  logic [2:0]                axi_slave_aw_size_i,
   input  logic [1:0]                axi_slave_aw_burst_i,
   input  logic                      axi_slave_aw_lock_i,
   input  logic [3:0]                axi_slave_aw_cache_i,
   input  logic [3:0]                axi_slave_aw_qos_i,
   input  logic [AXI_ID_WIDTH-1:0]   axi_slave_aw_id_i,
   input  logic [AXI_USER_WIDTH-1:0] axi_slave_aw_user_i,
   output logic                      axi_slave_aw_ready_o,

   // WRITE DATA CHANNEL
   input  logic                      axi_slave_w_valid_i,
   input  logic [AXI_DATA_WIDTH-1:0] axi_slave_w_data_i,
   input  logic [AXI_STRB_WIDTH-1:0] axi_slave_w_strb_i,
   input  logic [AXI_USER_WIDTH-1:0] axi_slave_w_user_i,
   input  logic                      axi_slave_w_last_i,
   output logic                      axi_slave_w_ready_o,

   // READ DATA CHANNEL
   output logic                      axi_slave_r_valid_o,
   output logic [AXI_DATA_WIDTH-1:0] axi_slave_r_data_o,
   output logic [1:0]                axi_slave_r_resp_o,
   output logic                      axi_slave_r_last_o,
   output logic [AXI_ID_WIDTH-1:0]   axi_slave_r_id_o,
   output logic [AXI_USER_WIDTH-1:0] axi_slave_r_user_o,
   input  logic                      axi_slave_r_ready_i,

   // WRITE RESPONSE CHANNEL
   output logic                      axi_slave_b_valid_o,
   output logic [1:0]                axi_slave_b_resp_o,
   output logic [AXI_ID_WIDTH-1:0]   axi_slave_b_id_o,
   output logic [AXI_USER_WIDTH-1:0] axi_slave_b_user_o,
   input  logic                      axi_slave_b_ready_i,

   // TCDM MASTER
   //***************************************
   // REQUEST CHANNEL
   output logic [3:0]                tcdm_master_req_o,
   output logic [3:0][31:0]          tcdm_master_add_o,
   output logic [3:0]                tcdm_master_type_o,
   output logic [3:0][3:0]           tcdm_master_be_o,
   output logic [3:0][31:0]          tcdm_master_data_o,
   input  logic [3:0]                tcdm_master_gnt_i,

   // RESPONSE CHANNEL
   input  logic [3:0]                tcdm_master_r_valid_i,
   input  logic [3:0][31:0]          tcdm_master_r_data_i,

   // BUSY SIGNAL
   output logic                      busy_o
);


endmodule