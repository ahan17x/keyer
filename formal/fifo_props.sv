// Formal properties for loom_fifo (bound with the DUT in fifo.sby).
`default_nettype none
module fifo_props #(parameter WIDTH = 4, parameter DEPTH = 4, parameter AW = 2) (
    input wire clk, input wire rst_n, input wire clear, input wire push,
    input wire [WIDTH-1:0] wr_data, input wire pop, input wire [WIDTH-1:0] rd_data,
    input wire empty, input wire full, input wire [AW:0] count
);
    loom_fifo #(.WIDTH(WIDTH), .DEPTH(DEPTH), .AW(AW)) dut (
        .clk(clk), .rst_n(rst_n), .clear(clear), .push(push), .wr_data(wr_data),
        .pop(pop), .rd_data(rd_data), .empty(empty), .full(full), .count(count));

    reg init = 1'b1;
    always @(posedge clk) init <= 1'b0;
    always @(*) if (init) assume(!rst_n);

    // occupancy bookkeeping: count tracks pushes minus pops exactly
    reg [AW:0] model_count;
    always @(posedge clk) begin
        if (!rst_n || clear) model_count <= 0;
        else model_count <= model_count + (push && !full) - (pop && !empty);
    end
    always @(*) if (!init) begin
        assert(count == model_count);
        assert(count <= DEPTH);
        assert(empty == (count == 0));
        assert(full == (count == DEPTH));
        assert(!(empty && full));
    end

    // data integrity: an arbitrary pushed word comes out after exactly the
    // number of entries ahead of it have been popped, unchanged.
    (* anyconst *) reg [WIDTH-1:0] token;
    reg tracking;
    reg [AW:0] ahead;
    always @(posedge clk) begin
        if (!rst_n || clear) begin tracking <= 0; ahead <= 0; end
        else begin
            if (!tracking && push && !full && wr_data == token) begin
                tracking <= 1;
                ahead <= count - (pop && !empty);
            end else if (tracking) begin
                if (pop && !empty) begin
                    if (ahead == 0) tracking <= 0;
                    else ahead <= ahead - 1;
                end
            end
        end
    end
`ifdef DATA_CHECK
    always @(*) if (!init && tracking && ahead == 0) begin
        assert(!empty);
        assert(rd_data == token);
    end
`endif
endmodule
